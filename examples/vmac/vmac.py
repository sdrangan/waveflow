"""``VmacAccel`` — the VMAC accelerator as a synthesizable ``HwComponent`` + the bit-exact
Python golden, modeled on :mod:`examples.shared_mem.hist` (the histogram).

VMAC is **complex-only** — every element is an interleaved ``re`` / ``im`` pair.  A VMAC
instance is parameterized entirely by its **structural** widths — fixed at synthesis, the HLS
template params, so ``A_T`` / ``ACC_T`` / ``OUT_T`` are compile-time (no dynamic types) —
declared as ``HwParam[int]`` fields: ``mem_dwidth`` (MEM_BW, sets the lane/packing factor),
``mem_awidth``, ``data_bw`` (IN_BW component width), ``int_bits`` (the ``F_in`` split),
``acc_bw``, ``out_bw``, ``q_rnd``, ``o_sat``.  The instance's values drive the command schema
through the computed :pyattr:`Cmd` property — the **instance → type bridge** that unifies the
component's ``HwParam`` with the schema's symbolic ``Param``::

    accel = VmacAccel(data_bw=16, int_bits=8)  # HwParam values bind at instantiation
    cmd   = accel.Cmd(...)                     # VmacCmd.specialize(mem_awidth=…, data_bw=…)
    dst   = accel.execute(cmd, mem)            # the bit-exact golden (instance method)

The op is one of three **element-wise** complex ops over a row-major shared-memory region —
``scalar_mult`` (``α[i]·A``), ``inner_prod`` (``A·conj(B)``), ``sum`` (``A+B``) — with an
optional row reduction ``Y[j] = Σ_i R[i, j]``.  The **single** synthesizable hook is
:meth:`vmac_compute` — read operands, op, requantize, write — whose **Python body is the
golden** (it delegates to :meth:`execute`) and whose **C++ is hand-written** in
``vmac_compute_impl.tpp`` (linked via ``@synthesizable(impl_file=…)``).
This mirrors :class:`~examples.shared_mem.hist.HistAccel`: declare the structure, hand-write
the compute, with a golden that auto-checks it.

The golden :meth:`execute` composes the merged integer-backed numpy ``ComplexField``
operators (``cmult`` / ``cadd`` / ``conj``), the wide-accumulator column reduction
:func:`~waveflow.hw.complexfield.csum` when ``reduce`` is set, and the output requantize
(right-shift + round + saturate) via the ``ap_fixed``-exact integer requantizer.  The
datapath: **element-wise op → wide accumulate (full precision, ≤ acc_bw) → right-shift →
round + saturate → write (out_bw)**.  The right-shift is the single lossy step (an ``ap_fixed``
assignment); its amount ``SHIFT = F_acc − F_in`` is *derived* from the flags + the structural
format (not in the command — see :meth:`output_format` / :meth:`derived_shift`), so the golden
is bit-exact with the Vitis kernel.  ``mem`` is the shared memory: a 1-D structured
``[('re','im')]`` array of stored ints; operands are row-major regions
``M[i, j] = mem[addr + i·row_stride + j]`` (columns unit-stride).  :meth:`execute` writes the
requantized result into ``mem`` at the ``d`` region (so commands compose) and returns the dst
``DataArray``.

Constructed without a ``sim``, ``VmacAccel`` is a lightweight params + golden object (no
``Simulation`` needed) — the form the golden / numeric tests use.  With a ``sim`` it is the
full ``HwComponent`` with a single ``m_axi`` (``MMIFMaster``) port that serves **both** the
AXI-MM command queue and the A/B/Y data (Stage 1 of ``plans/vmac_mm_queue_timing.md``):
:meth:`run_proc` is the loosely-timed SimPy execution model — a free-running queue consumer
that issues timed ``m_mem`` transactions around the bit-exact golden.  (The synthesizable C++
remains the hand-written ``vmac_compute`` hook; the cosim codegen path is unchanged.)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar

import numpy as np

from examples.vmac.vmac_cmd import OpCode, VmacCmd
from waveflow.hw.clock import Clock
from waveflow.hw.complexfield import ComplexField, cadd, cmult, conj, csum
from waveflow.hw.dataschema import DataArray
from waveflow.hw.fixpoint import FixedField
from waveflow.hw.hw_component import HwComponent, HwParam
from waveflow.hw.aximm_queue import AXIMMQueue
from waveflow.hw.arrayutils import read_array as wf_read_array
from waveflow.hw.arrayutils import write_array as wf_write_array
from waveflow.hw.memif import MMIFMaster
from waveflow.named import NamedObject
from waveflow.simulation.logger import NullLogger
from waveflow.hw.synth import synthesizable
from waveflow.simulation.simobj import ProcessGen
from waveflow.utils import complexutils as cx
from waveflow.utils import fixputils
from waveflow.utils.fixputils import Format, OMode, QMode, add_format, sum_format


@dataclass
class VmacAccel(HwComponent):
    """A VMAC accelerator: structural ``HwParam`` widths + the Python golden + the
    ``vmac_compute`` synthesizable hook (the codegen source for ``gen/vmac.cpp``)."""

    cpp_kernel_name: ClassVar[str | None] = "vmac"
    cpp_namespace: ClassVar[str | None] = "vmac_impl"

    # structural params (synthesis-time HLS template params).  Everything that sizes or
    # types the datapath lives here — so A_T / ACC_T / OUT_T are compile-time (no dynamic
    # types).  q_rnd / o_sat MUST be structural (ap_fixed's Q/O modes are compile-time).
    mem_dwidth: HwParam[int] = 512  # MEM_BW (memory-interface width / lane factor)
    mem_awidth: HwParam[int] = 32  # m_axi address width
    data_bw: HwParam[int] = 16  # IN_BW — operand (re/im) component width
    int_bits: HwParam[int] = 8  # I of the operand format (F_in = data_bw - int_bits)
    acc_bw: HwParam[int] = 48  # accumulator width budget
    out_bw: HwParam[int] = 16  # writeback (re/im) component width
    q_rnd: HwParam[int] = 0  # output rounding: 0 = AP_TRN, 1 = AP_RND
    o_sat: HwParam[int] = 0  # output overflow: 0 = AP_WRAP, 1 = AP_SAT
    clk: Clock = field(default_factory=lambda: Clock(freq=1e9))

    def __post_init__(self) -> None:
        if self.sim is None:
            # params + golden mode — usable without a Simulation (the m_axi port +
            # run_proc, which need a sim, are set up in the else branch).
            self._wrap_hw_params()
            NamedObject.__post_init__(self)
            object.__setattr__(self, "_hw_construction_complete", True)
        else:
            super().__post_init__()
            # Stage 1 (loosely-timed SimPy model): ONE m_mem master serves both the
            # command dequeue (over an AXI-MM queue) and the A/B/Y data — one realistic
            # AXI port.  ``mem_dwidth`` is the sim bus width here (must be a queue-legal
            # 8/16/32/64); for synthesis it is the lane-packing MEM_BW (set per build).
            self.m_mem = MMIFMaster(
                name=f"{self.name}_m_mem", sim=self.sim, bitwidth=int(self.mem_dwidth)
            )
            self.add_endpoint(self.m_mem)
            # The command queue + the data-region byte base are attached by the driver
            # (it owns the system memory map) before ``run_sim``.
            self.cmd_queue: AXIMMQueue | None = None
            self.data_base: int = 0
            # Stage 2 timeline capture (attached by the driver; default = no-op).
            self.logger = NullLogger()
            self.region_labels: dict[int, str] = {}  # elem offset -> A / B / Y_anorm / ...
            self.txns: list[dict] = []               # per memory transaction
            self.cmd_records: list[dict] = []         # per command (dequeue/complete/latency)
            self.q_events: list[dict] = []            # ring dequeue events (for occupancy)

    @property
    def Cmd(self) -> type[VmacCmd]:
        """The command schema specialized to this accelerator's widths — the instance → type
        bridge from ``HwParam`` values to the schema's ``Param`` specialization."""
        return VmacCmd.specialize(
            mem_awidth=int(self.mem_awidth), data_bw=int(self.data_bw)
        )

    @property
    def q_mode(self) -> QMode:
        """The (structural) output quantization mode — ``ap_fixed``'s compile-time ``Q``."""
        return QMode.AP_RND if int(self.q_rnd) else QMode.AP_TRN

    @property
    def o_mode(self) -> OMode:
        """The (structural) output overflow mode — ``ap_fixed``'s compile-time ``O``."""
        return OMode.AP_SAT if int(self.o_sat) else OMode.AP_WRAP

    # --- private datapath helpers (complex-only → static / class) ----------------
    @staticmethod
    def _fixed_cls(fmt: Format) -> type[FixedField]:
        return FixedField.specialize(
            fmt.W, fmt.int_bits, fmt.signed, fmt.q_mode, fmt.o_mode
        )

    @staticmethod
    def _region_idx(reg, n_rows: int, n_cols: int) -> np.ndarray:
        """The row-major index matrix: ``addr + i·row_stride + j`` (columns unit-stride)."""
        rows = np.arange(n_rows)[:, None] * int(reg.row_stride)
        cols = np.arange(n_cols)[None, :]
        return int(reg.addr) + rows + cols

    @classmethod
    def _operand(cls, M: np.ndarray, in_fmt: Format) -> DataArray:
        """Wrap a strided complex matrix view (structured re/im) as a DataArray operand."""
        elem = ComplexField.specialize(cls._fixed_cls(in_fmt))
        shape = M.shape if M.shape else (1,)
        return DataArray.specialize(elem, max_shape=tuple(shape))(M)

    def _data_elem(self) -> type[ComplexField]:
        """The shared-memory element type — the operand ``ComplexField`` at the input
        format.  Both VMAC and the host (de)serialize the A/B/Y regions through it, so the
        timed ``m_mem`` words round-trip bit-exactly with the golden's structured buffer."""
        return ComplexField.specialize(self._fixed_cls(self._in_fmt()))

    @classmethod
    def _alpha(
        cls, sc, n_rows: int, n_cols: int, in_fmt: Format, mem: np.ndarray
    ) -> DataArray:
        """Build the ``scalar_mult`` alpha operand as an ``(n_rows, n_cols)`` broadcast: direct
        immediate (one value, broadcast over every row/col) or per-row indirect (``alpha[i]`` at
        ``addr + i·stride``, broadcast over columns)."""
        if bool(sc.direct):
            re = np.full((n_rows, n_cols), int(cx.re_of(sc.imm)), dtype=np.int64)
            im = np.full((n_rows, n_cols), int(cx.im_of(sc.imm)), dtype=np.int64)
            M = cx.make_complex(re, im, in_fmt)
        else:
            idx = int(sc.addr) + np.arange(n_rows) * int(sc.stride)
            col = mem[idx]  # (n_rows,) structured complex
            M = np.broadcast_to(col[:, None], (n_rows, n_cols)).copy()
        return cls._operand(M, in_fmt)

    @staticmethod
    def _requantize(t: DataArray, out_cls: type[FixedField]) -> DataArray:
        """Requantize the wide complex accumulator ``t`` into the output ``FixedField``
        ``out_cls`` — the single lossy step (right-shift + round + saturate), an ``ap_fixed``
        assignment via the ``ap_fixed``-exact integer requantizer (``fixputils.quantize``).
        The shift is implicit in the binary-point difference src.frac − target.frac."""
        target = out_cls.get_format()
        src = t.element_type.inner_format()
        re = fixputils.quantize(cx.re_of(t.val), src, target)
        im = fixputils.quantize(cx.im_of(t.val), src, target)
        elem = ComplexField.specialize(out_cls)
        struct = cx.make_complex(re, im, target)
        return DataArray.specialize(elem, max_shape=struct.shape)(struct)

    @staticmethod
    def _writeback(mem: np.ndarray, reg, dst: DataArray) -> None:
        val = np.asarray(dst.val)
        if val.ndim == 1:  # reduced -> single row of columns
            idx = int(reg.addr) + np.arange(val.shape[0])  # columns unit-stride
        else:
            idx = VmacAccel._region_idx(reg, val.shape[0], val.shape[1])
        mem[idx] = val

    # --- numeric model: datapath format derivation (the codegen spec) ------------
    def _in_fmt(self) -> Format:
        """The operand component ``FixedField`` format — fully structural: ``W = data_bw``,
        ``I = int_bits``, so ``F_in = data_bw - int_bits`` fractional bits."""
        return Format(int(self.data_bw), int(self.int_bits), signed=True)

    def accumulator_format(self, cmd: VmacCmd) -> Format:
        """The wide-accumulator component ``FixedField`` for ``cmd`` — full precision, no loss.

        Complex-only, so a multiply is ``cmult_format`` (``sub_format``-based, +1 integer bit)
        and an add is ``add_format`` (+1 integer bit).  Composes the datapath as pure format
        algebra (no data), per op:

        - **scalar_mult** ``alpha·A``: one multiply → ``cmult_format(in, in)`` (``F_acc = 2·F_in``);
        - **inner_prod** ``A·conj(B)``: ``conj`` grows the inner ``(W+1, I+1)``, then a multiply
          → ``cmult_format(in, conj(in))`` (``F_acc = 2·F_in``);
        - **sum** ``A+B``: aligned add → ``add_format(in, in)`` (``F_acc = F_in``);
        - **reduce**: ``+⌈log₂ n_rows⌉`` integer bits (``sum_format``).

        ``F_acc`` (fractional depth) depends only on the op (``2·F_in`` for the products, ``F_in``
        for the add); this is what makes the requantize shift derivable (see
        :meth:`output_format` / :meth:`derived_shift`)."""
        in_fmt = self._in_fmt()
        op = OpCode(int(cmd.op))
        if op is OpCode.scalar_mult:
            acc = cx.cmult_format(in_fmt, in_fmt)  # alpha · A
        elif op is OpCode.inner_prod:
            acc = cx.cmult_format(in_fmt, cx.conj_format(in_fmt))  # A · conj(B)
        else:  # sum
            acc = add_format(in_fmt, in_fmt)  # A + B (aligned, +1 int bit)
        if bool(cmd.reduce):
            acc = sum_format(acc, int(cmd.n_rows))  # + ceil(log2 n_rows) int bits
        return acc

    def output_format(self, cmd: VmacCmd) -> type[FixedField]:
        """The output (per-component) ``FixedField`` for ``cmd`` — structural scale, the
        codegen target.

        The output is fixed at the **input fractional scale** ``F_out = F_in`` (a normalized
        result comparable to the inputs), with structural width ``out_bw`` and round/saturate
        modes ``q_mode`` / ``o_mode``.  The single lossy step requantizes the accumulator
        (``F_acc`` fractional bits) down to ``F_out``, i.e. a right-shift of
        ``SHIFT = F_acc − F_in`` (= ``F_in`` for the product ops, ``0`` for ``sum``) — *derived*
        from the op, not carried in the command.  Fail-loud on a mis-sized accelerator: an
        accumulator wider than ``acc_bw``, or an ``out_bw`` too small to hold the integer part.
        """
        acc = self.accumulator_format(cmd)
        if acc.W > int(self.acc_bw):
            raise ValueError(
                f"accumulator width {acc.W} exceeds acc_bw={int(self.acc_bw)}; widen acc_bw."
            )
        out_frac = int(self.data_bw) - int(self.int_bits)  # F_out = F_in (structural)
        out_bw = int(self.out_bw)
        int_bits = out_bw - out_frac
        if int_bits < 0:
            raise ValueError(
                f"out_bw={out_bw} is too small: F_out={out_frac} fractional bits exceed it "
                f"(need out_bw >= {out_frac})."
            )
        return FixedField.specialize(
            out_bw, int_bits, acc.signed, self.q_mode, self.o_mode
        )

    def derived_shift(self, cmd: VmacCmd) -> int:
        """The requantize right-shift derived from the op + structural format —
        ``SHIFT = F_acc − F_out`` (``F_in`` for the product ops, ``0`` for ``sum``; the residual
        runtime numeric).  The ``.tpp`` reproduces it via the ``ap_fixed`` requantize scale.
        """
        return self.accumulator_format(cmd).frac_bits - (
            int(self.data_bw) - int(self.int_bits)
        )

    # --- the golden ----------------------------------------------------------
    def execute(self, cmd: VmacCmd, mem: np.ndarray) -> DataArray:
        """Execute a ``VmacCmd`` over ``mem``; write the dst region and return the dst array.

        This is the synchronous golden — the Python body of the :meth:`vmac_compute` hook
        (the ``.tpp`` reproduces it bit-for-bit in C++).  Complex-only; the format is read off
        ``self`` (structural), not the command.  The numeric / golden tests call it directly
        on a no-``sim`` accelerator."""
        in_fmt = self._in_fmt()
        n, m = int(cmd.n_rows), int(cmd.n_cols)
        mem = np.asarray(mem)
        out_cls = self.output_format(cmd)  # fail-loud config guards (acc_bw / out_bw)

        def region(reg) -> DataArray:
            return self._operand(mem[self._region_idx(reg, n, m)], in_fmt)

        # element-wise op -> R[i, j]
        op = OpCode(int(cmd.op))
        a = region(cmd.a)
        if op is OpCode.scalar_mult:
            t = cmult(self._alpha(cmd.alpha, n, m, in_fmt, mem), a)  # alpha[i] · A
        elif op is OpCode.inner_prod:
            t = cmult(a, conj(region(cmd.b)))  # A · conj(B)
        else:  # sum
            t = cadd(a, region(cmd.b))  # A + B

        # optional row reduction (wide accumulator)
        if bool(cmd.reduce):
            t = csum(t, axis=0)

        # invariant: the actual accumulator format must equal the derived spec (the format
        # algebra in accumulator_format mirrors the operators it composes).
        actual = t.element_type.inner_format()
        spec = self.accumulator_format(cmd)
        if (actual.W, actual.int_bits, actual.signed) != (
            spec.W,
            spec.int_bits,
            spec.signed,
        ):
            raise AssertionError(
                f"accumulator format mismatch: actual {actual} != derived {spec}"
            )

        dst = self._requantize(t, out_cls)
        self._writeback(mem, cmd.y, dst)
        return dst

    # --- the synthesizable hook + kernel body --------------------------------
    @synthesizable(impl_file="vmac_compute_impl.tpp")
    def vmac_compute(self, cmd: VmacCmd, mem: np.ndarray) -> ProcessGen[DataArray]:
        """The element-wise op (``scalar_mult`` / ``inner_prod`` / ``sum``, optional row
        reduce) — the **single** hook (the whole datapath: read operands, op, requantize, write).

        Its Python body is the golden (delegates to :meth:`execute`); its C++ is the
        hand-written ``vmac_compute_impl.tpp`` (the ``ap_fixed`` accumulator =
        :meth:`accumulator_format`, output = :meth:`output_format`).  As a
        ``@synthesizable`` hook the body is not extracted — codegen emits a call to the
        ``.tpp`` function — so the SimPy model may freely use the full-precision numpy golden.
        """
        return self.execute(cmd, mem)
        yield  # unreachable — makes this a generator (ProcessGen)

    def _record_txn(
        self, cmd_idx: int, name: str, rw: str, addr: int, nwords: int,
        tstart: float, tend: float, ab_eq: bool,
    ) -> None:
        """Append one captured memory transaction (and mirror it to the raw CSV log)."""
        self.txns.append({
            "cmd_idx": int(cmd_idx), "name": name, "rw": rw, "addr": int(addr),
            "nwords": int(nwords), "tstart": float(tstart), "tend": float(tend),
            "ab_eq": bool(ab_eq),
        })
        self.logger.log(
            role="vmac", event="mem", cmd_idx=cmd_idx, label=name, rw=rw,
            nwords=int(nwords), addr=int(addr), tstart=float(tstart), tend=float(tend),
            ab_eq=int(ab_eq),
        )

    def _read_region(
        self, local: np.ndarray, elem_addr: int, count: int,
        elem: type, elem_words: int, elem_bytes: int, mem_bw: int,
        cmd_idx: int, ab_eq: bool,
    ) -> ProcessGen[None]:
        """Issue ONE timed ``m_mem`` read of *count* contiguous complex elements at
        *elem_addr* (element index in the data region) and unpack them into ``local``;
        capture (t_start, t_end, nwords, addr, label) of the LT block."""
        nwords = count * elem_words
        addr = self.data_base + elem_addr * elem_bytes
        t0 = self.now
        words = yield from self.m_mem.read(nwords, addr)
        t1 = self.now
        local[elem_addr:elem_addr + count] = wf_read_array(
            words, elem, mem_bw, shape=count
        ).val
        self._record_txn(
            cmd_idx, self.region_labels.get(elem_addr, "data"), "read",
            addr, nwords, t0, t1, ab_eq,
        )

    def run_proc(self) -> ProcessGen[None]:
        """Loosely-timed SimPy execution model (Stage 1): a free-running queue consumer.

        Dequeue a :class:`VmacCmd` from the AXI-MM command queue; on ``OpCode.end``
        stop.  Otherwise issue **timed ``m_mem`` reads** of the operands (A; B unless
        ``ab_eq``) and a **timed write** of Y — one whole-matrix LT block per operand —
        with the numpy golden (:meth:`execute`) producing the values into a local buffer.

        Latency is **transaction-driven** here: ``ab_eq`` (``b_addr == a_addr``) suppresses
        B's read, so the model predicts ``anorm`` faster than ``abcorr``.  That is the
        intended Stage-1 behaviour — the gap vs. the fixed-II RTL (equal latency, freed bus)
        is the headline finding the Stage-3 cosim calibration exposes; do not equalize it
        here.  The synthesizable C++ is unchanged (the ``.tpp`` hook); this is the *sim*
        model, which did not exist before."""
        if self.cmd_queue is None:
            raise RuntimeError(
                "VmacAccel.run_proc requires an attached cmd_queue (set by the driver "
                "before run_sim)."
            )
        mem_bw = int(self.m_mem.bitwidth)
        word_bytes = mem_bw // 8
        in_fmt = self._in_fmt()
        elem = self._data_elem()
        elem_words = elem.nwords_per_inst(mem_bw)
        elem_bytes = elem_words * word_bytes

        # poll the ring at the bus clock (1 cycle), not the queue default of 1.0 s, so the
        # consumer wakes promptly relative to the realistic clock (see vmac_queue_sim).
        poll = 1.0 / float(self.clk.freq)

        cmd_idx = -1
        while True:
            cmd: VmacCmd = yield from self.cmd_queue.get(self.Cmd, poll_interval=poll)
            cmd_idx += 1
            dequeue_t = self.now
            self.q_events.append(
                {"t": float(dequeue_t), "kind": "dequeue", "cmd_idx": cmd_idx}
            )
            self.logger.log(role="vmac", event="dequeue", cmd_idx=cmd_idx,
                            tstart=float(dequeue_t))
            op = OpCode(int(cmd.op))
            if op is OpCode.end:
                break  # sentinel: the host's last command; drain and terminate

            n, m = int(cmd.n_rows), int(cmd.n_cols)
            nm = n * m
            need_b = op in (OpCode.inner_prod, OpCode.sum)
            # ab_eq: B aliases A — skip B's read (it is not needed in the golden, and the
            # suppressed bus beat is the point).  Latency stays transaction-driven.
            ab_eq = need_b and int(cmd.b.addr) == int(cmd.a.addr)
            alpha_indirect = op is OpCode.scalar_mult and not bool(cmd.alpha.direct)

            ends = [int(cmd.a.addr) + nm, int(cmd.y.addr) + nm]
            if need_b:
                ends.append(int(cmd.b.addr) + nm)
            if alpha_indirect:
                ends.append(int(cmd.alpha.addr) + n)
            extent = max(ends)
            local = cx.make_complex(np.zeros(extent), np.zeros(extent), in_fmt)

            # timed operand reads — one whole-matrix LT block each
            yield from self._read_region(
                local, int(cmd.a.addr), nm, elem, elem_words, elem_bytes, mem_bw,
                cmd_idx, ab_eq,
            )
            if need_b and not ab_eq:
                yield from self._read_region(
                    local, int(cmd.b.addr), nm, elem, elem_words, elem_bytes, mem_bw,
                    cmd_idx, ab_eq,
                )
            if alpha_indirect:
                yield from self._read_region(
                    local, int(cmd.alpha.addr), n, elem, elem_words, elem_bytes, mem_bw,
                    cmd_idx, ab_eq,
                )

            # the bit-exact golden computes R / reduce / requantize and writes Y into `local`
            dst = self.execute(cmd, local)

            # timed Y writeback — one LT block
            y_words = wf_write_array(dst, word_bw=mem_bw)
            y_addr = self.data_base + int(cmd.y.addr) * elem_bytes
            t0 = self.now
            yield from self.m_mem.write(y_words, y_addr)
            t1 = self.now
            self._record_txn(
                cmd_idx, self.region_labels.get(int(cmd.y.addr), "Y"), "write",
                y_addr, len(y_words), t0, t1, ab_eq,
            )

            complete_t = self.now
            self.cmd_records.append({
                "cmd_idx": cmd_idx, "op": op.name, "ab_eq": bool(ab_eq),
                "n_rows": n, "n_cols": m,
                "dequeue_t": float(dequeue_t), "complete_t": float(complete_t),
                "latency": float(complete_t - dequeue_t),
            })
