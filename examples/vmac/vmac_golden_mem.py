"""Harness plumbing: apply VMAC's one golden (:meth:`VmacAccel.execute`) to a memory image.

This is the histogram-anatomy marshaller for VMAC (cf. ``HistController`` in
``examples/shared_mem/hist.py``): slice the operand regions out of a flat, **element-indexed**
structured-complex image per the command's region descriptors, run the pure golden
``VmacAccel.execute``, and write the result region back.

It is **testbench / vector-generator plumbing — not a second golden**: all the math lives in
``execute``.  The build + cosim vector generators share this one helper so none of them re-rolls the
region addressing.  (The accelerator itself now exposes only ``execute`` — the addressing lives here
in the harness, the way the histogram keeps marshalling in its controller, not on the model.)
"""
from __future__ import annotations

import numpy as np

from examples.vmac.vmac_cmd import OpCode


def _region_idx(reg, n_rows: int, n_cols: int) -> np.ndarray:
    """The row-major index matrix ``addr + i·row_stride + j`` (columns unit-stride) — element
    coordinates into a flat, element-indexed image."""
    rows = np.arange(n_rows)[:, None] * int(reg.row_stride)
    cols = np.arange(n_cols)[None, :]
    return int(reg.addr) + rows + cols


def apply_golden(accel, cmd, mem: np.ndarray):
    """Run ``accel.execute`` over operands sliced from the element-indexed image *mem*, write the
    dst region back into *mem* (in place), and return the dst ``DataArray``.

    *mem* is a 1-D structured-complex array; ``cmd.{a,b,y,alpha}`` carry element-coordinate region
    descriptors.  Mirrors the operand selection in ``execute`` (which op needs B / an indirect
    alpha) — the irreducible "what to read from memory for this command" the harness must know,
    exactly as ``HistController`` knows data/edges/counts."""
    mem = np.asarray(mem)
    n, m = int(cmd.n_rows), int(cmd.n_cols)
    op = OpCode(int(cmd.op))

    a = mem[_region_idx(cmd.a, n, m)]
    b = mem[_region_idx(cmd.b, n, m)] if op in (OpCode.inner_prod, OpCode.sum) else None
    alpha = None
    if op is OpCode.scalar_mult and not bool(cmd.alpha.direct):
        alpha = mem[int(cmd.alpha.addr) + np.arange(n) * int(cmd.alpha.stride)]

    dst = accel.execute(cmd, a, b, alpha)

    val = np.asarray(dst.val)
    if val.ndim == 1:  # reduced -> single row of columns (unit-stride)
        mem[int(cmd.y.addr) + np.arange(val.shape[0])] = val
    else:
        mem[_region_idx(cmd.y, val.shape[0], val.shape[1])] = val
    return dst
