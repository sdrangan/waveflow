"""VMAC data types — the command schema **and** the fixed-point derived types.

This module holds *everything VMAC's datapath is typed by*, kept apart from the accelerator
behaviour (``examples/vmac/vmac.py``) so the example reads in the order users should follow:
**define the data types → define the interfaces over them → define the module**.  Two halves:

* the runtime **command schema** (``VmacCmd`` + ``OpCode`` / ``Region`` / ``Alpha``), below;
* :class:`VmacFormats` — the **dependent types** a parametrized fixed-point engine induces
  (operand element type, wide accumulator format, output format, the requantize shift), each a
  pure function of the structural widths.  ``VmacAccel`` composes one as ``accel.types`` and the
  datapath/golden read their formats off it, so the accelerator class carries the *behaviour*,
  not the type algebra.

---

``VmacCmd`` — the VMAC instruction DataSchema (runtime tier).

A configurable complex vector engine (VMAC) executes one of three **element-wise** complex
ops over a row-major region of shared memory, with an optional row reduction::

    scalar_mult :  R[i, j] = alpha[i] · A[i, j]
    inner_prod  :  R[i, j] = A[i, j] · conj(B[i, j])
    sum         :  R[i, j] = A[i, j] + B[i, j]
    reduce      :  Y[j] = Σ_i R[i, j]   (else Y[i, j] = R[i, j])

VMAC is **complex-only** (every element is an interleaved ``re`` / ``im`` pair of
``data_bw``-bit stored ints); there is no real mode.

Parameters split into two tiers (see :class:`~examples.vmac.vmac.VmacAccel`): everything that
**sizes or types the datapath** is **structural** and lives on the accelerator as ``HwParam``
(``mem_dwidth`` / ``mem_awidth`` / ``data_bw`` / ``int_bits`` / ``acc_bw`` / ``out_bw`` /
``q_rnd`` / ``o_sat``), so ``A_T`` / ``ACC_T`` / ``OUT_T`` are compile-time — no dynamic types.
The **runtime** instruction lives here in ``VmacCmd`` and carries only the **op + geometry**:
the ``op`` selector, the ``reduce`` flag, the matrix shape (``n_rows`` / ``n_cols``), the
row-major operand / destination regions (``a`` always; ``b`` for ``inner_prod`` / ``sum``;
``y`` always — addr + row_stride pitch), and the ``alpha`` scalar (``scalar_mult`` only:
direct immediate or per-row indirect pointer + stride, indexed by row ``i``).  **No format
fields** — the requantize shift is *derived* from the op + the structural format.

The width-bearing fields are set by the accelerator that produces/consumes the command
(``addr`` is ``mem_awidth`` bits; the immediate complex ``imm`` is a ``data_bw``-per-component
``ComplexField``).  These schemas are **Level-2 declarative**
(:class:`~waveflow.hw.dataschema.ParamSchema`): each declares :class:`~waveflow.hw.param.Param`
attributes and a dict-literal ``elements`` that references them directly — the core
``IntField.specialize`` / ``Region.specialize`` calls defer on the symbolic params, and
``specialize(**vals)`` resolves + caches (with the ``Region`` / ``Alpha`` cascade sharing
params).  ``VmacCmd`` is still a plain ``DataList``, so it serializes / deserializes and
code-generates like any schema.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

import numpy as np

from waveflow.hw import BooleanField, EnumField, IntField, Param, ParamSchema
from waveflow.hw.complexfield import ComplexField
from waveflow.hw.dataschema import DataArray
from waveflow.hw.fixpoint import FixedField
from waveflow.utils import complexutils as cx
from waveflow.utils import fixputils
from waveflow.utils.fixputils import Format, OMode, QMode, add_format, sum_format

# --- field aliases (names match the auto-generated IntField subclass __name__) ----
UInt16 = IntField.specialize(16, signed=False)


class OpCode(IntEnum):
    """The VMAC element-wise op selector (the only runtime *control*; loop-invariant)."""

    scalar_mult = 0  # R = alpha[i] · A
    inner_prod = 1  # R = A · conj(B)
    sum = 2  # R = A + B
    end = 3  # sentinel: no compute — a free-running queue consumer breaks its loop on this


class Region(ParamSchema):
    """A row-major operand region: ``M[i, j] = mem[addr + i·row_stride + j]``.

    Columns are the **contiguous, unit-stride packed inner dimension** — the wide bus reads
    ``pf = MEM_BW / element_bits`` contiguous elements per cycle, so an arbitrary column
    stride would only defeat it (scattered reads → one element/beat).  ``row_stride`` is the
    outer **pitch** (in elements) between successive rows, letting a region be a sub-matrix of
    a larger buffer."""

    mem_awidth = Param(32)
    elements = {
        "addr": {
            "schema": IntField.specialize(mem_awidth, signed=False),
            "description": "base offset into shared memory",
        },
        "row_stride": {
            "schema": IntField.specialize(mem_awidth, signed=True),
            "description": "outer pitch (in elements) between successive rows",
        },
    }


class Alpha(ParamSchema):
    """The ``scalar_mult`` scaling operand ``alpha``: a direct complex immediate, or per-row
    indirect (pointer ``addr`` + ``stride``; element ``i`` is read at ``addr + i·stride``,
    ``stride 0`` broadcasts).

    The direct immediate is a single :class:`~waveflow.hw.complexfield.ComplexField` element
    (one interleaved re/im pair of stored ints, ``data_bw`` bits each), matching the indirect
    path's complex element so the kernel reads one complex code with no re/im reconstruction.
    The inner stays a raw ``IntField`` (stored codes; the command is format-free — ``int_bits``
    is structural), packed re low / im high."""

    mem_awidth = Param(32)
    data_bw = Param(32)
    elements = {
        "direct": {
            "schema": BooleanField,
            "description": "True = immediate complex imm; False = indirect addr/stride",
        },
        # immediate complex stored-code (re low, im high; data_bw bits per component)
        "imm": ComplexField.specialize(IntField.specialize(data_bw, signed=True)),
        "addr": IntField.specialize(mem_awidth, signed=False),
        "stride": IntField.specialize(mem_awidth, signed=True),
    }


class VmacCmd(ParamSchema):
    """The VMAC instruction — the runtime tier (see module docstring).

    Region/scalar field widths track the accelerator's ``mem_awidth`` (addresses) and
    ``data_bw`` (immediates); the cascade runs through ``Region`` / ``Alpha`` specialize
    (both share ``mem_awidth`` with ``VmacCmd`` and resolve to the same cached classes).
    """

    mem_awidth = Param(32)
    data_bw = Param(32)
    elements = {
        # the element-wise op + the optional row reduction (the only runtime control).
        "op": {
            "schema": EnumField.specialize(OpCode),
            "description": "element-wise op: scalar_mult / inner_prod / sum",
        },
        "reduce": {
            "schema": BooleanField,
            "description": "sum the rows (per-column reduction)",
        },
        # matrix shape (operands share it; dst is (1, n_cols) when reduced).
        "n_rows": UInt16,
        "n_cols": UInt16,
        # row-major operand / destination regions (cascade: share mem_awidth).
        "a": {
            "schema": Region.specialize(mem_awidth=mem_awidth),
            "description": "operand A region (always read)",
        },
        "b": {
            "schema": Region.specialize(mem_awidth=mem_awidth),
            "description": "operand B region (read for inner_prod / sum)",
        },
        "y": {
            "schema": Region.specialize(mem_awidth=mem_awidth),
            "description": "destination Y region (reduced to a single row when reduce)",
        },
        # the scalar_mult scaling operand (cascade: share mem_awidth and data_bw).
        "alpha": Alpha.specialize(mem_awidth=mem_awidth, data_bw=data_bw),
    }


# ---------------------------------------------------------------------------
# Fixed-point derived types — the "dependent types" of the parametrized datapath
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class VmacFormats:
    """The fixed-point derived types/formats VMAC's datapath depends on — a pure function of the
    structural widths.

    A parametrized fixed-point engine induces **dependent types**: the operand element type, the
    wide (lossless) accumulator format, and the requantized output type all follow from
    ``data_bw`` / ``int_bits`` / ``acc_bw`` / ``out_bw`` plus the rounding/overflow modes.  This
    is that pattern made explicit — composed from the framework's format-growth algebra
    (``cmult_format`` / ``add_format`` / ``sum_format`` / ``conj_format``), not re-derived by hand
    — and lifted out of ``VmacAccel`` so the accelerator carries *behaviour*, not type algebra.
    ``VmacAccel`` exposes one as ``accel.types``."""

    data_bw: int
    int_bits: int
    acc_bw: int
    out_bw: int
    q_rnd: int
    o_sat: int

    # -- rounding / overflow modes (structural ap_fixed Q / O) ----------------
    @property
    def q_mode(self) -> QMode:
        """The (structural) output quantization mode — ``ap_fixed``'s compile-time ``Q``."""
        return QMode.AP_RND if int(self.q_rnd) else QMode.AP_TRN

    @property
    def o_mode(self) -> OMode:
        """The (structural) output overflow mode — ``ap_fixed``'s compile-time ``O``."""
        return OMode.AP_SAT if int(self.o_sat) else OMode.AP_WRAP

    # -- element types --------------------------------------------------------
    def fixed_cls(self, fmt: Format) -> type[FixedField]:
        """The ``FixedField`` subclass for a component ``Format`` (the single specialize call)."""
        return FixedField.specialize(fmt.W, fmt.int_bits, fmt.signed, fmt.q_mode, fmt.o_mode)

    def in_format(self) -> Format:
        """The operand component ``FixedField`` format — fully structural: ``W = data_bw``,
        ``I = int_bits``, so ``F_in = data_bw - int_bits`` fractional bits."""
        return Format(int(self.data_bw), int(self.int_bits), signed=True)

    def operand_elem(self) -> type[ComplexField]:
        """The shared-memory element type — the operand ``ComplexField`` at the input format.
        VMAC and the host (de)serialize the A/B/Y regions through it, so the timed ``m_mem`` words
        round-trip bit-exactly with the golden's structured buffer."""
        return ComplexField.specialize(self.fixed_cls(self.in_format()))

    def operand(self, M: np.ndarray) -> DataArray:
        """Wrap a strided complex matrix view (structured re/im) as a ``DataArray`` operand."""
        elem = ComplexField.specialize(self.fixed_cls(self.in_format()))
        shape = M.shape if M.shape else (1,)
        return DataArray.specialize(elem, max_shape=tuple(shape))(M)

    def alpha(self, sc, n_rows: int, n_cols: int, alpha_arr) -> DataArray:
        """Build the ``scalar_mult`` alpha operand as an ``(n_rows, n_cols)`` broadcast: direct
        immediate (one value broadcast over every row/col) or per-row indirect — the pre-read
        per-row column ``alpha_arr`` (shape ``(n_rows,)``) broadcast over columns.  Does not read
        memory: the shell / vector generator supplies the already-fetched ``alpha_arr``."""
        in_fmt = self.in_format()
        if bool(sc.direct):
            re = np.full((n_rows, n_cols), int(cx.re_of(sc.imm)), dtype=np.int64)
            im = np.full((n_rows, n_cols), int(cx.im_of(sc.imm)), dtype=np.int64)
            M = cx.make_complex(re, im, in_fmt)
        else:
            col = np.asarray(alpha_arr).reshape(n_rows)  # (n_rows,) structured complex
            M = np.broadcast_to(col[:, None], (n_rows, n_cols)).copy()
        return self.operand(M)

    # -- format algebra (the datapath's deliberate fixed-point growth) --------
    def accumulator_format(self, cmd) -> Format:
        """The wide-accumulator component ``Format`` for ``cmd`` — full precision, no loss.

        Complex-only: a multiply is ``cmult_format`` (+1 integer bit), an add is ``add_format``
        (+1 integer bit), and ``reduce`` adds ``⌈log₂ n_rows⌉`` integer bits (``sum_format``):

        - **scalar_mult** ``alpha·A``: ``cmult_format(in, in)`` (``F_acc = 2·F_in``);
        - **inner_prod** ``A·conj(B)``: ``cmult_format(in, conj(in))`` (``F_acc = 2·F_in``);
        - **sum** ``A+B``: ``add_format(in, in)`` (``F_acc = F_in``).

        ``F_acc`` depends only on the op, which is what makes the requantize shift derivable (see
        :meth:`output_format` / :meth:`derived_shift`)."""
        in_fmt = self.in_format()
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

    def output_format(self, cmd) -> type[FixedField]:
        """The output (per-component) ``FixedField`` for ``cmd`` — structural scale, the codegen
        target.  Output is fixed at the input fractional scale ``F_out = F_in`` with structural
        width ``out_bw`` and modes ``q_mode`` / ``o_mode``.  Fail-loud on a mis-sized accelerator:
        an accumulator wider than ``acc_bw``, or an ``out_bw`` too small for the integer part."""
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
        return FixedField.specialize(out_bw, int_bits, acc.signed, self.q_mode, self.o_mode)

    def derived_shift(self, cmd) -> int:
        """The requantize right-shift derived from the op + structural format —
        ``SHIFT = F_acc − F_out`` (``F_in`` for the product ops, ``0`` for ``sum``)."""
        return self.accumulator_format(cmd).frac_bits - (
            int(self.data_bw) - int(self.int_bits)
        )

    @staticmethod
    def requantize(t: DataArray, out_cls: type[FixedField]) -> DataArray:
        """Requantize the wide complex accumulator ``t`` into the output ``FixedField`` ``out_cls``
        — the single lossy step (right-shift + round + saturate) via the ``ap_fixed``-exact integer
        requantizer (``fixputils.quantize``); the shift is implicit in ``src.frac − target.frac``."""
        target = out_cls.get_format()
        src = t.element_type.inner_format()
        re = fixputils.quantize(cx.re_of(t.val), src, target)
        im = fixputils.quantize(cx.im_of(t.val), src, target)
        elem = ComplexField.specialize(out_cls)
        struct = cx.make_complex(re, im, target)
        return DataArray.specialize(elem, max_shape=struct.shape)(struct)

    # -- memory packing -------------------------------------------------------
    def lane_capacity(self, word_bw: int) -> int:
        """Complex columns packed per ``word_bw``-bit memory word — ``word_bw // element_bits``
        (element = ``2 · data_bw``).  The Python twin of the C++ ``lane_capacity<W>()``; sets the
        kernel ``PF`` and the ``trips = n_rows · ⌈n_cols / PF⌉`` timing step."""
        return max(1, int(word_bw) // (2 * int(self.data_bw)))
