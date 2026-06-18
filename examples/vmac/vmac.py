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
full ``HwComponent``: an ``m_axi`` (``MMIFMaster``) data port + an ``s_in`` command stream,
with :meth:`run_proc` the kernel body the generated kernel is lowered from (the m_axi data
plumbing + codegen are wired in the build phase).
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
from waveflow.hw.interface import StreamIFSlave
from waveflow.hw.memif import MMIFMaster
from waveflow.named import NamedObject
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
            # The m_axi data port (the shared memory the op reads/writes) and the
            # command stream; mirror HistAccel's endpoint set.
            self.s_in = StreamIFSlave(
                name=f"{self.name}_s_in", sim=self.sim, bitwidth=int(self.mem_dwidth)
            )
            self.m_mem = MMIFMaster(
                name=f"{self.name}_m_mem", sim=self.sim, bitwidth=int(self.mem_dwidth)
            )
            for ep in (self.s_in, self.m_mem):
                self.add_endpoint(ep)

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

    def run_proc(self) -> ProcessGen[None]:
        """Kernel body (single ap_ctrl_hs invocation) — the codegen root.

        Read one :class:`VmacCmd` off ``s_in``, then run the op in the
        :meth:`vmac_compute` hook against the ``m_mem`` shared memory.  The m_axi
        read/write plumbing around the hook (the strided gather/scatter the
        ``.tpp`` performs lane-by-lane) and the codegen wiring are completed in the
        build phase; this is the structure the generated kernel is lowered from."""
        cmd: VmacCmd = yield from self.s_in.get(self.Cmd)
        yield from self.vmac_compute(cmd, self.m_mem)
