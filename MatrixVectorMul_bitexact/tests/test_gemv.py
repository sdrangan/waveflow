"""S1 gate -- the Python gemv model against the real Vitis BLAS kernel.

Golden: ``golden/gemv_f32_M4_N64_P4.txt``, produced by ``cpp/dump_gemv.cpp``, which instantiates
``xf::blas::gemv`` itself.  Checked in, so these tests need neither Vitis nor a compiler.

Everything is compared as IEEE-754 bit patterns.  A decimal comparison would hide exactly the
1-ULP differences this kernel's reduction order produces, which is the whole subject.
"""
from __future__ import annotations

import struct
from pathlib import Path

import numpy as np
import pytest

from MatrixVectorMul_bitexact.wf_gemv.gemv import (adder_delays, binary_sum, dot, f32_bits, gemv)

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "golden" / "gemv_f32_M4_N64_P4.txt"
PAR_ENTRIES = 4


def _rows(path: Path) -> list[list[str]]:
    return [ln.split() for ln in path.read_text().splitlines()
            if ln.strip() and not ln.lstrip().startswith("#")]


@pytest.fixture(scope="module")
def data():
    ni = _rows(ROOT / "data" / "input.txt")
    n_case, m, n = (int(v) for v in ni[0])
    vals = [int(v[0]) for v in ni[1:]]
    f32 = lambda u: struct.unpack("<f", struct.pack("<I", u))[0]  # noqa: E731
    cases, off = [], 0
    for _ in range(n_case):
        A = np.array([f32(u) for u in vals[off:off + m * n]], dtype=np.float32).reshape(m, n)
        off += m * n
        x = np.array([f32(u) for u in vals[off:off + n]], dtype=np.float32)
        off += n
        cases.append((A, x))
    hw = [int(v[0]) for v in _rows(GOLDEN)[1:]]
    return {"cases": cases, "hw": hw, "M": m, "N": n}


def _naive_tree(a, b):
    """The plausible wrong model: one binary tree over all N products."""
    v = [np.float32(a[i] * b[i]) for i in range(len(a))]
    while len(v) > 1:
        v = [np.float32(v[i] + v[i + 1]) for i in range(0, len(v), 2)]
    return v[0]


def test_golden_shape(data):
    """Guard the golden -- a short regen would make every other test vacuous."""
    assert data["M"] == 4 and data["N"] == 64
    assert len(data["cases"]) == 8
    assert len(data["hw"]) == 8 * data["M"]


def test_adder_delays_matches_the_library():
    """``AdderDelay<T>`` -- a property of the element type, and it changes the answer."""
    assert adder_delays("float32") == 4
    assert adder_delays("float64") == 8
    assert adder_delays("int32") == 1


def test_binary_sum_requires_power_of_two():
    """``BinarySum<T,N>`` halves until it reaches 1; a non-power-of-two would silently misgroup."""
    assert binary_sum([1.0, 2.0, 3.0, 4.0]) == np.float32(10.0)
    with pytest.raises(ValueError, match="power-of-two"):
        binary_sum([1.0, 2.0, 3.0])


def test_gemv_is_bit_exact(data):
    """THE S1 GATE: the model equals xf::blas::gemv, bit for bit, on every case."""
    bad = 0
    for k, (A, x) in enumerate(data["cases"]):
        got = gemv(A, x, PAR_ENTRIES)
        want = data["hw"][k * data["M"]:(k + 1) * data["M"]]
        bad += sum(1 for r in range(data["M"]) if f32_bits(got[r]) != want[r])
    assert bad == 0, f"{bad} of {len(data['hw'])} rows differ from the hardware"


def test_a_naive_tree_would_be_wrong(data):
    """Teeth: a single binary tree over all N products is the obvious model, and it is wrong.

    The library trees *within* a beat and *within* a chunk of beats, but accumulates *across*
    chunks sequentially.  If this ever stops failing, the chunked accumulation has been
    "simplified" into a plain tree and the model is silently wrong on ~1 row in 5.
    """
    bad = 0
    for k, (A, x) in enumerate(data["cases"]):
        want = data["hw"][k * data["M"]:(k + 1) * data["M"]]
        bad += sum(1 for r in range(data["M"])
                   if f32_bits(_naive_tree(A[r], x)) != want[r])
    assert bad > 0, "the naive tree matched everywhere; the goldens no longer discriminate"


def test_numpy_dot_would_be_wrong(data):
    """Teeth: ``numpy.dot`` -- what anyone would reach for first -- disagrees on half the rows.

    Worth pinning because numpy is *unreliably* right: it matched this kernel at N=64 with simple
    data while differing at N=16, so a small test suite would have blessed it.
    """
    bad = 0
    for k, (A, x) in enumerate(data["cases"]):
        want = data["hw"][k * data["M"]:(k + 1) * data["M"]]
        bad += sum(1 for r in range(data["M"]) if f32_bits(np.dot(A[r], x)) != want[r])
    assert bad > 0, "numpy.dot matched everywhere; the goldens no longer discriminate"


def test_par_entries_changes_the_result(data):
    """``t_LogParEntries`` is part of the numerical contract, not just a throughput knob.

    A design that retunes the stream width changes its output bits.  Recording that here means a
    model that ignored the parameter could not pass.
    """
    A, x = data["cases"][4]                     # a discriminating case
    assert any(f32_bits(dot(A[r], x, 4)) != f32_bits(dot(A[r], x, 8))
               for r in range(data["M"])), "parEntries had no effect; the model ignores it"


def test_dot_rejects_a_bad_length():
    with pytest.raises(ValueError, match="multiple of parEntries"):
        dot(np.ones(6, dtype=np.float32), np.ones(6, dtype=np.float32), 4)
