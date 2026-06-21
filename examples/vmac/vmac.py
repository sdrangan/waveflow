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
    dst   = accel.execute(cmd, a, b, alpha)    # the pure bit-exact golden (operand arrays in)

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
is bit-exact with the Vitis kernel.  The shared memory is a 1-D structured ``[('re','im')]``
array of stored ints; operands are row-major regions ``M[i, j] = mem[addr + i·row_stride + j]``
(columns unit-stride).  :meth:`execute` is the **pure** golden — dense operand arrays
(``a, b, alpha``) in, the requantized dst ``DataArray`` out, no memory/addressing/writeback.
The synthesizable shell :meth:`vmac_compute` owns the memory access (pipelined ``m_mem``
reads/writes) and supplies the operands; :meth:`execute_mem` is the synchronous flat-memory
twin (image in → image out, used by the build / cosim vector generators).

Constructed without a ``sim``, ``VmacAccel`` is a lightweight params + golden object (no
``Simulation`` needed) — the form the golden / numeric tests use.  With a ``sim`` it is the
full ``HwComponent`` with a single ``m_axi`` (``MMIFMaster``) port that serves **both** the
AXI-MM command queue and the A/B/Y data (Stage 1 of ``plans/vmac_mm_queue_timing.md``):
:meth:`run_proc` is the loosely-timed SimPy execution model — a free-running queue consumer
that issues timed ``m_mem`` transactions around the bit-exact golden.  (The synthesizable C++
remains the hand-written ``vmac_compute`` hook; the cosim codegen path is unchanged.)
"""

from __future__ import annotations

import math
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
from waveflow.hw.memif import MMIFMaster
from waveflow.named import NamedObject
from waveflow.simulation.logger import NullLogger
from waveflow.hw.synth import sim_only, synthesizable
from waveflow.simulation.simobj import ProcessGen
from waveflow.utils import complexutils as cx
from waveflow.utils import fixputils
from waveflow.utils.fixputils import Format, OMode, QMode, add_format, sum_format


#: Command-ring poll interval in **bus cycles**.  A poll interval now *means*
#: something (the LT polling-overhead model in ``plans/poll_until_lt_model.md``):
#: the consumer's empty-ring wait goes through :meth:`MMIFMaster.poll_until`,
#: which charges ``1/poll_interval`` of bus occupancy (derating real transfers)
#: and adds the ``(poll_interval-1)/2``-cycle discovery delay.  This replaces the
#: old structural ``poll_cycles=64`` band-aid — an aggressive (tiny) interval now
#: shows up as derated bus throughput, not just shifted dequeue times.
RING_POLL_CYCLES: int = 64


@dataclass(frozen=True)
class VmacTiming:
    """Calibrated pipeline-schedule parameters for the **II-decoupled** timing model (Stage 5
    of ``plans/vmac_mm_queue_timing.md``).

    The loosely-timed Stage-1 model was *transaction-gated* — a command's latency was the sum
    of its transfer blocks — so it mispredicted the ``ab_eq`` ordering (anorm faster) and
    underestimated absolute latency ~5x.  Under II-decoupling the latency is instead the kernel
    **pipeline schedule**

        latency_cycles = depth + ii * trips,    trips = n_rows * ceil(n_cols / pf)

    fit from the Vitis cosim sweep (:mod:`examples.vmac.vmac_calibrate`).  ``depth`` is the
    fill/drain + reduce-init/writeback overhead; ``ii`` is the (memory-gated) cycles per
    inner-loop trip.  Loaded programmatically by the driver — never hard-coded in the model —
    and attached as ``VmacAccel.timing``."""

    depth: float
    ii: float

    def cycles(self, trips: int) -> float:
        return self.depth + self.ii * trips


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
            # Stage 5 II-decoupling: the calibrated pipeline-schedule params.  ``None`` ->
            # the Stage-1 *transaction-gated* model (latency = Σ transfer blocks, kept so the
            # naive-vs-calibrated comparison can render both).  Set by the driver to a
            # ``VmacTiming`` (loaded from the committed cosim calibration) to make latency the
            # II-driven pipeline schedule while occupancy stays transaction-driven.
            self.timing: VmacTiming | None = None
            # Stage 2 timeline capture (attached by the driver; default = no-op).
            self.logger = NullLogger()
            self.region_labels: dict[int, str] = {}  # elem offset -> A / B / Y_anorm / ...
            self.txns: list[dict] = []               # per memory transaction
            self.cmd_records: list[dict] = []         # per command (dequeue/complete/latency)
            self.q_events: list[dict] = []            # ring dequeue events (for occupancy)

    def pre_sim(self) -> None:
        """Sim setup (never synthesized): validate the driver wiring, cache the structural
        element geometry, and precompute the coarse ring-poll interval + the command counter.

        Everything here is one-time Python introspection / setup — not hardware — so it lives
        out of :meth:`run_proc` (the HLS extraction target); ``run_proc`` then reads only the
        cached structural constants (``_elem`` / ``_mem_bw``)."""
        super().pre_sim()
        if self.cmd_queue is None:
            raise RuntimeError(
                "VmacAccel.run_proc requires an attached cmd_queue (set by the driver "
                "before run_sim)."
            )
        # element geometry in the shared data region (structural, constant) — cached on self so
        # both the consumer and the vmac_compute shell share one element->byte-address helper.
        self._mem_bw = int(self.m_mem.bitwidth)
        self._elem = self._data_elem()
        # ring poll interval (bus cycles), set once on the queue (D3) so the synthesizable
        # call site stays the bare get(self.Cmd); the poll lives in the C++ dequeue hook.  The
        # consumer's empty-wait now goes through MMIFMaster.poll_until: the poll's bus cost is
        # modeled (occupancy derating + discovery delay), so an aggressive interval is no longer
        # a 1-cycle bus saturation the model can't see (it replaces the poll_cycles band-aid).
        self.cmd_queue.poll_interval = float(RING_POLL_CYCLES)
        self._cmd_idx = -1  # sim-only command counter, maintained by the @sim_only records
        self._dequeue_t = 0.0  # sim-only: dequeue time stashed by _record_dequeue

    @property
    def Cmd(self) -> type[VmacCmd]:
        """The command schema specialized to this accelerator's widths — the instance → type
        bridge from ``HwParam`` values to the schema's ``Param`` specialization."""
        return VmacCmd.specialize(
            mem_awidth=int(self.mem_awidth), data_bw=int(self.data_bw)
        )

    @property
    def pf(self) -> int:
        """Complex columns packed per memory word — the kernel's ``PF = MEM_BW / element_bits``
        (element = ``2 * data_bw``).  Sets ``trips = n_rows * ceil(n_cols / pf)`` in the
        II-decoupled timing model, matching the ``.tpp`` inner-loop step (``col0 += PF``)."""
        return max(1, int(self.mem_dwidth) // (2 * int(self.data_bw)))

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
        cls, sc, n_rows: int, n_cols: int, in_fmt: Format, alpha_arr
    ) -> DataArray:
        """Build the ``scalar_mult`` alpha operand as an ``(n_rows, n_cols)`` broadcast: direct
        immediate (one value, broadcast over every row/col) or per-row indirect — the pre-read
        per-row column ``alpha_arr`` (shape ``(n_rows,)``) broadcast over columns.  It no longer
        reads memory: the shell (:meth:`vmac_compute` / :meth:`execute_mem`) supplies the already
        fetched ``alpha_arr``."""
        if bool(sc.direct):
            re = np.full((n_rows, n_cols), int(cx.re_of(sc.imm)), dtype=np.int64)
            im = np.full((n_rows, n_cols), int(cx.im_of(sc.imm)), dtype=np.int64)
            M = cx.make_complex(re, im, in_fmt)
        else:
            col = np.asarray(alpha_arr).reshape(n_rows)  # (n_rows,) structured complex
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
    def execute(self, cmd: VmacCmd, a, b=None, alpha=None) -> DataArray:
        """The **pure-math golden**: dense operand arrays in, dst ``DataArray`` out — no memory,
        addressing, writeback, or timing (the shell :meth:`vmac_compute` owns those and supplies
        the already-read operands).

        This is the bit-exact datapath (the ``.tpp`` reproduces it for-bit in C++).  Complex-only;
        the format is read off ``self`` (structural), not the command.  ``a`` / ``b`` are the A / B
        regions as ``n_rows·n_cols`` structured complex elements (reshaped to ``(n, m)`` here);
        ``alpha`` is the per-row indirect column (``(n_rows,)``) for ``scalar_mult`` (``None`` for a
        direct immediate, ``None`` for the other ops).  The numeric / golden tests call it directly
        on a no-``sim`` accelerator; :meth:`execute_mem` is the flat-memory twin for the build /
        cosim vector generators."""
        in_fmt = self._in_fmt()
        n, m = int(cmd.n_rows), int(cmd.n_cols)
        out_cls = self.output_format(cmd)  # fail-loud config guards (acc_bw / out_bw)
        A = self._operand(np.asarray(a).reshape(n, m), in_fmt)

        # element-wise op -> R[i, j]
        op = OpCode(int(cmd.op))
        if op is OpCode.scalar_mult:
            t = cmult(self._alpha(cmd.alpha, n, m, in_fmt, alpha), A)  # alpha[i] · A
        elif op is OpCode.inner_prod:
            B = self._operand(np.asarray(b).reshape(n, m), in_fmt)
            t = cmult(A, conj(B))  # A · conj(B)
        else:  # sum
            B = self._operand(np.asarray(b).reshape(n, m), in_fmt)
            t = cadd(A, B)  # A + B

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

        return self._requantize(t, out_cls)  # NOTE: no writeback — the shell stores Y

    # NOTE: the flat-memory golden image (operands in → expected image out) is now harness
    # plumbing, not a method here: ``examples/vmac/vmac_golden_mem.py::apply_golden`` slices the
    # regions from an element-indexed image and runs ``execute`` (the one golden).  The
    # accelerator's golden surface is just ``execute`` — the histogram anatomy (golden on the
    # model, marshalling in the testbench).

    # --- the synthesizable shell + kernel body -------------------------------
    @synthesizable(impl_file="vmac_compute_impl.tpp")
    def vmac_compute(self, cmd: VmacCmd, mem) -> ProcessGen[DataArray]:
        """The **synthesizable shell**: it owns the (pipelined) memory access + transaction
        capture and delegates the math to the pure golden :meth:`execute`.

        ``mem`` is the memory the kernel reads/writes — in the SimPy model the
        :class:`MMIFMaster` (``self.m_mem``); in the generated C++ the ``m_axi`` pointer.  Same
        arg, same position: **do not reorder** — ``@synthesizable`` maps this signature to the C++
        call ``vmac_compute(cmd, mem)``.  As a ``@synthesizable`` hook the Python body is *not*
        extracted (codegen emits a call to the hand-written ``vmac_compute_impl.tpp``), so this
        sim body may freely use pipelined ``m_mem`` reads + the full-precision numpy golden.

        Reads each operand region as one contiguous block (``row_stride == n_cols``, the standing
        assumption) and reshapes in :meth:`execute`; suppresses B's read when it aliases A
        (``ab_eq``); records each transfer (:meth:`_record_txn`) as it follows the reads/writes.
        The command-level II-decoupling pad lives here too (after the writeback): since
        :meth:`run_proc` dispatches this immediately on dequeue, the entry time equals the
        dequeue time, so per-command latency is unchanged — and because this body is not
        extracted, the (non-synthesizable) ``yield self.timeout`` pad is excluded for free."""
        t_entry = self.now  # == dequeue_t (run_proc dispatches on the dequeue tick)
        n, m = int(cmd.n_rows), int(cmd.n_cols)
        nm = n * m
        op = OpCode(int(cmd.op))
        need_b = op in (OpCode.inner_prod, OpCode.sum)
        # ab_eq: B aliases A — skip B's read (not needed by the golden; the suppressed bus beat
        # is the point).  alpha_indirect: per-row alpha lives in memory, not the immediate.
        ab_eq = need_b and int(cmd.b.addr) == int(cmd.a.addr)
        alpha_indirect = op is OpCode.scalar_mult and not bool(cmd.alpha.direct)
        cmd_idx = self._cmd_idx

        # The shared data region as an element-indexed view (the framework owns the element→byte
        # conversion — see MMIFMaster.region; the C++ hook uses read_array_lane/slice over the same
        # element coordinates).  All addresses below are element indices straight from the command.
        data = mem.region(self.data_base, self._elem, word_bw=self._mem_bw)

        # timed operand reads — one whole-matrix LT block each
        a, t0, t1, nw = yield from data.read_pipelined(int(cmd.a.addr), int(cmd.a.addr) + nm)
        self._record_txn(cmd_idx, self.region_labels.get(int(cmd.a.addr), "data"),
                         "read", data.byte_of(int(cmd.a.addr)), nw, t0, t1, ab_eq)
        b = None
        if need_b and not ab_eq:
            b, t0, t1, nw = yield from data.read_pipelined(int(cmd.b.addr), int(cmd.b.addr) + nm)
            self._record_txn(cmd_idx, self.region_labels.get(int(cmd.b.addr), "data"),
                             "read", data.byte_of(int(cmd.b.addr)), nw, t0, t1, ab_eq)
        elif ab_eq:
            b = a
        alpha = None
        if alpha_indirect:
            alpha, t0, t1, nw = yield from data.read_pipelined(
                int(cmd.alpha.addr), int(cmd.alpha.addr) + n)
            self._record_txn(cmd_idx, self.region_labels.get(int(cmd.alpha.addr), "data"),
                             "read", data.byte_of(int(cmd.alpha.addr)), nw, t0, t1, ab_eq)

        # the bit-exact golden computes R / reduce / requantize (pure math)
        dst = self.execute(cmd, a, b, alpha)

        # timed Y writeback — one LT block (element type is dst's, the output format)
        t0, t1, nw = yield from data.write_pipelined(
            int(cmd.y.addr), dst, element_type=type(dst).element_type)
        self._record_txn(cmd_idx, self.region_labels.get(int(cmd.y.addr), "Y"),
                         "write", data.byte_of(int(cmd.y.addr)), nw, t0, t1, ab_eq)

        # II-decoupling: advance to the calibrated pipeline-schedule completion time.  The
        # transfers above already moved `now` (their bus occupancy is real); pad the remainder so
        # the command latency is the II-driven schedule, NOT the transfer sum.  trips depend only
        # on geometry, so anorm and abcorr (equal trips) get equal latency — the fixed-II RTL
        # behaviour the transaction-gated model could not represent.
        if self.timing is not None:
            trips = n * math.ceil(m / self.pf)
            sched_secs = self.timing.cycles(trips) / float(self.clk.freq)
            pad = sched_secs - (self.now - t_entry)
            if pad > 0:
                yield self.timeout(pad)
        return dst

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

    @sim_only
    def _record_dequeue(self, cmd: VmacCmd) -> None:
        """Sim-only: bump the command counter, capture + stash the dequeue time, and record the
        dequeue event (occupancy curve + raw log).  Runs before the datapath so ``self._cmd_idx``
        is current for the transaction records :meth:`vmac_compute` emits, and ``self._dequeue_t``
        is stashed for :meth:`_record_command` (so ``run_proc``'s body holds no ``self.now``)."""
        self._cmd_idx += 1
        t = self.now
        self._dequeue_t = float(t)
        self.q_events.append(
            {"t": float(t), "kind": "dequeue", "cmd_idx": self._cmd_idx}
        )
        self.logger.log(role="vmac", event="dequeue", cmd_idx=self._cmd_idx,
                        tstart=float(t))

    @sim_only
    def _record_command(self, cmd: VmacCmd) -> None:
        """Sim-only: capture the per-command record (dequeue→complete latency) after the
        datapath returns, reading the dequeue time stashed by :meth:`_record_dequeue`."""
        dequeue_t = self._dequeue_t
        complete_t = self.now
        op = OpCode(int(cmd.op))
        need_b = op in (OpCode.inner_prod, OpCode.sum)
        ab_eq = need_b and int(cmd.b.addr) == int(cmd.a.addr)
        self.cmd_records.append({
            "cmd_idx": self._cmd_idx, "op": op.name, "ab_eq": bool(ab_eq),
            "n_rows": int(cmd.n_rows), "n_cols": int(cmd.n_cols),
            "dequeue_t": float(dequeue_t), "complete_t": float(complete_t),
            "latency": float(complete_t - dequeue_t),
        })

    def run_proc(self) -> ProcessGen[None]:
        """Free-running queue consumer — the HLS extraction target.

        The loop body is **hardware control flow only**: poll/dequeue a :class:`VmacCmd` from the
        AXI-MM command ring, dispatch the per-command datapath to the synthesizable
        :meth:`vmac_compute` shell, and stop on ``OpCode.end``.  Everything sim-only lives
        elsewhere so the loop reads like the RTL: the precondition + structural geometry +
        coarse poll in :meth:`pre_sim`; the dequeue/command bookkeeping (and the ``cmd_idx``
        counter) in the ``@sim_only`` :meth:`_record_dequeue` / :meth:`_record_command` helpers
        (silently dropped by the extractor); the II-decoupling pad inside the non-extracted
        ``vmac_compute`` body.

        The loop body extracts to the HLS IR (``HwStmtExtractor``): the dequeue is the
        synthesizable :meth:`AXIMMQueue.get <waveflow.hw.aximm_queue.AXIMMQueue.get>`
        (``AXIMMQueueGetStmt`` → the ring-dequeue hook), the ``end`` sentinel is the
        ``cmd.op == OpCode.end`` case, and the datapath is the ``vmac_compute`` hook.  See the
        timing model in :meth:`vmac_compute`: bus transactions are always issued so ``ab_eq``
        shows anorm at half the reads, while latency is the II-driven pipeline schedule."""
        while True:
            cmd: VmacCmd = yield from self.cmd_queue.get(self.Cmd)
            self._record_dequeue(cmd)               # @sim_only (bumps _cmd_idx, stashes dequeue_t)
            if cmd.op == OpCode.end:
                return  # sentinel: the host's last command; drain and terminate
            yield from self.vmac_compute(cmd, self.m_mem)
            self._record_command(cmd)               # @sim_only
