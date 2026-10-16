"""The double-precision gate -- ``dot_tree`` with ``AdderDelay = 8``.

``double`` takes the same reduction as ``float`` but groups beats by 8 rather than 4, and every
rounding happens at 53 bits instead of 24.

This file exists because of a real defect: ``dot`` hardcoded ``float32`` while ``adder_delays``
already answered 8 for ``float64``, so a caller asking for double got plausible, **silently
wrong** numbers.  Nothing in the suite could catch that, because nothing ran double against the
library.  Golden from ``cpp/dump_gemv_f64.cpp``; checked in, so no Vitis and no compiler needed.
"""
from __future__ import annotations

import pathlib
import struct

import numpy as np
import pytest

from MatrixVectorMul_bitexact.wf_gemv.gemv import adder_delays, binary_sum, dot, gemv

ROOT = pathlib.Path(__file__).resolve().parents[1]
SIZE = (3, 128)


def _payload(path: pathlib.Path) -> list[str]:
    return [ln for ln in path.read_text().splitlines()
            if ln.strip() and not ln.lstrip().startswith("#")]


def _f64(pattern: int) -> np.float64:
    return np.float64(struct.unpack("<d", struct.pack("<Q", int(pattern)))[0])


def _bits(d) -> int:
    return struct.unpack("<Q", struct.pack("<d", np.float64(d)))[0]


@pytest.fixture(scope="module")
def loaded():
    m, n = SIZE
    lines = _payload(ROOT / "data" / f"input_f64_M{m}_N{n}.txt")
    n_case, fm, fn = (int(v) for v in lines[0].split())
    assert (fm, fn) == (m, n)
    vals, pos, cases = [int(v) for v in lines[1:]], 0, []
    for _ in range(n_case):
        a = np.array([_f64(v) for v in vals[pos:pos + m * n]]).reshape(m, n); pos += m * n
        x = np.array([_f64(v) for v in vals[pos:pos + n]]); pos += n
        cases.append((a, x))
    assert pos == len(vals), "input file has trailing values"
    rows = [tuple(int(v) for v in ln.split()[:3]) + (int(ln.split()[3], 16),)
            for ln in _payload(ROOT / "golden" / f"gemv_f64_M{m}_N{n}.txt")[1:]]
    return m, n, cases, rows


def test_adder_delay_for_double_is_eight():
    """It is 8 for double and 4 for float -- a property of the type, and it changes the answer."""
    assert adder_delays("float64") == 8
    assert adder_delays("float32") == 4


def test_binary_sum_honours_the_dtype():
    """``binary_sum`` must round at the element type's precision, not always at float32.

    ``0.1 + 0.2`` differs between the two, so this fails if the dtype is ignored.
    """
    f32 = binary_sum([0.1, 0.2], np.float32)
    f64 = binary_sum([0.1, 0.2], np.float64)
    assert np.float64(f32) != f64, "float32 and float64 gave the same sum; the dtype is ignored"


def test_double_is_bit_exact(loaded):
    """THE GATE: the model equals xf::blas::gemv for double, at every swept stream width."""
    _m, _n, cases, rows = loaded
    assert rows, "empty golden"
    bad = []
    for logp, k, r, want in rows:
        a, x = cases[k]
        got = _bits(gemv(a, x, par_entries=1 << logp, dtype=np.float64)[r])
        if got != want:
            bad.append(f"logP={logp} case={k} row={r}: model {got:016x} != golden {want:016x}")
    assert not bad, f"{len(bad)}/{len(rows)} rows differ:\n" + "\n".join(bad[:8])


def test_running_double_data_as_float_would_be_wrong(loaded):
    """⚠️ The defect this file was written for.

    The model used to force float32 regardless of the data.  It still produces a number; it is
    just the wrong one.  Two separate things have to be right -- the 53-bit arithmetic and the
    8-beat grouping -- so this pins both by forcing each back to its float32 value.
    """
    _m, _n, cases, rows = loaded
    as_f32 = wrong_delays = 0
    for logp, k, r, want in rows:
        a, x = cases[k]
        as_f32 += _bits(np.float64(gemv(a, x, par_entries=1 << logp, dtype=np.float32)[r])) != want
        wrong_delays += _bits(dot(a[r], x, par_entries=1 << logp, delays=4,
                                  dtype=np.float64)) != want
    assert as_f32 > 0.5 * len(rows), (
        f"float32 arithmetic reproduced {len(rows) - as_f32}/{len(rows)} double rows; the data "
        f"no longer needs 53-bit precision to be distinguished")
    assert wrong_delays > 0, (
        "grouping by 4 instead of 8 matched every row; the data does not span enough groups")


def test_the_float_path_still_defaults_to_float32(loaded):
    """Adding double must not have moved the default out from under existing callers."""
    a = np.array([[1.0, 2.0, 3.0, 4.0]], dtype=np.float64)
    x = np.array([0.1, 0.2, 0.3, 0.4], dtype=np.float64)
    assert gemv(a, x).dtype == np.float32
    assert dot(a[0], x, 4).dtype == np.float32


def test_an_unsupported_dtype_is_refused():
    """``dot_tree`` is the float/double path.  Anything else takes ``dot_dsp`` and must not be
    quietly run through the wrong reduction."""
    with pytest.raises(NotImplementedError, match="dot_dsp"):
        dot(np.arange(4), np.arange(4), 4, dtype=np.int32)
