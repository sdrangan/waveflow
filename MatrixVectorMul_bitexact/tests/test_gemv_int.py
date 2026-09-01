"""S3 gate -- the non-float ``dot_dsp`` path.

``float``/``double`` go to ``dot_tree``; every other element type goes to ``dot_dsp``, which is a
plain sequential accumulation in index order.  So the two paths fail differently, and this file
pins the differences rather than assuming they carry over from the float gate.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from MatrixVectorMul_bitexact.wf_gemv.gemv import gemv_int

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "golden" / "gemv_int_M3_N32.txt"
INPUT = ROOT / "data" / "input_int_M3_N32.txt"
WIDTHS = {"i32": 32, "i16": 16}


def _rows(path: Path) -> list[list[str]]:
    return [ln.split() for ln in path.read_text().splitlines()
            if ln.strip() and not ln.lstrip().startswith("#")]


def _fit(v: int, w: int) -> int:
    """Narrow to the element type exactly as the C++ ``(T_elem)`` cast does."""
    return (int(v) + (1 << (w - 1))) % (1 << w) - (1 << (w - 1))


@pytest.fixture(scope="module")
def loaded() -> dict:
    ni = _rows(INPUT)
    n_case, m, n = (int(v) for v in ni[0])
    vals = [int(v[0]) for v in ni[1:]]
    cases, off = [], 0
    for _ in range(n_case):
        A = np.array(vals[off:off + m * n], dtype=np.int64).reshape(m, n)
        off += m * n
        x = np.array(vals[off:off + n], dtype=np.int64)
        off += n
        cases.append((A, x))
    return {"cases": cases, "M": m, "N": n, "golden": _rows(GOLDEN)[1:]}


def test_golden_shape(loaded):
    assert loaded["M"] == 3 and loaded["N"] == 32
    assert len(loaded["cases"]) == 5
    assert len(loaded["golden"]) == 5 * loaded["M"] * 5      # 5 (config, logP) runs


def test_non_float_gemv_is_bit_exact(loaded):
    """THE S3 GATE: the dot_dsp model equals the hardware on every row."""
    bad = 0
    for cfg, _lp, k, r, y in loaded["golden"]:
        w = WIDTHS[cfg]
        A, x = loaded["cases"][int(k)]
        el = np.array([_fit(v, w) for v in A[int(r)]], dtype=np.int64)
        xv = np.array([_fit(v, w) for v in x], dtype=np.int64)
        bad += gemv_int(el.reshape(1, -1), xv, w)[0] != int(y)
    assert bad == 0, f"{bad} of {len(loaded['golden'])} rows differ"


def test_par_entries_does_not_change_the_non_float_result(loaded):
    """The paths differ here, and the difference is worth pinning.

    On the float path ``t_LogParEntries`` is part of the numerical contract -- it sets the tree
    shape.  ``dot_dsp`` has one accumulator updated in index order, so the width is pure
    throughput and the bits do not move.  A model that applied the float path's parEntries
    handling here would be wrong, and this is what would catch it.
    """
    per_row: dict = {}
    for cfg, _lp, k, r, y in loaded["golden"]:
        per_row.setdefault((cfg, k, r), set()).add(int(y))
    varying = [key for key, vals in per_row.items() if len(vals) > 1]
    assert not varying, f"parEntries changed the result on the non-float path: {varying[:3]}"


def test_the_goldens_actually_overflow_the_accumulator(loaded):
    """Teeth: an unwrapped accumulator must be wrong, or the wrapping model is untested.

    ``t_MacDataType`` is pinned to the element type (it cannot be widened -- see below), so a
    long dot product wraps.  If this stops failing, the inputs no longer reach the accumulator
    limit and the gate above would pass for a model with no wrapping at all.
    """
    bad = 0
    for cfg, _lp, k, r, y in loaded["golden"]:
        w = WIDTHS[cfg]
        A, x = loaded["cases"][int(k)]
        exact = sum(_fit(p, w) * _fit(q, w) for p, q in zip(A[int(r)], x))
        bad += exact != int(y)
    assert bad > 0, "no row overflows; the goldens no longer exercise wrapping"


def test_wider_mac_type_is_uncompilable_in_the_library():
    """``t_MacDataType`` is exposed, documented -- and dead.  Recorded, not worked around.

    ``gemv`` declares ``p_y`` as ``hls::stream<WideType<t_DataType, 1>::t_TypeInt>`` and then
    forwards it to ``DotHelper<..., t_MacDataType>::dot``, which expects
    ``WideType<t_MacDataType, 1>``.  Any ``t_MacDataType != t_DataType`` therefore fails to
    compile *inside* ``gemv.hpp:47``, on both the 5-arg and 8-arg overloads, identically in
    2023.1 and 2025.1.

    This test asserts the source still has that shape, so if AMD fixes it the model's
    single-``width`` assumption is flagged rather than silently becoming wrong.
    """
    hdr = Path("/home/marco/AmirProjects/Vitis_Libraries_2025.1/blas/L1/include/hw/xf_blas/gemv.hpp")
    if not hdr.exists():
        pytest.skip("Vitis BLAS source not present")
    text = hdr.read_text()
    assert "WideType<t_DataType, 1>::t_TypeInt>& p_y" in text, (
        "gemv's output stream is no longer element-typed -- t_MacDataType may now work, so "
        "gemv_int's single-width assumption needs revisiting")
    assert "t_MacDataType>::dot" in text, "gemv no longer forwards t_MacDataType"
