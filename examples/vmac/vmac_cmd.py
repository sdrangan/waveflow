"""``VmacCmd`` — the VMAC instruction DataSchema (runtime tier).

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

from enum import IntEnum

from waveflow.hw import BooleanField, EnumField, IntField, Param, ParamSchema
from waveflow.hw.complexfield import ComplexField

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
