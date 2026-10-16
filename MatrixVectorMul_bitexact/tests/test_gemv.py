"""S1/S2 gates -- the Python gemv model against the real Vitis BLAS kernel.

Goldens in ``golden/`` are produced by ``cpp/dump_gemv.cpp``, which instantiates
``xf::blas::gemv`` itself and sweeps ``logParEntries`` 0..4.  Checked in, so these tests need
neither Vitis nor a compiler.

Everything is compared as IEEE-754 bit patterns.  A decimal comparison would hide exactly the
1-ULP differences this kernel's reduction order produces, which is the whole subject.
"""
from __future__ import annotations

import struct
from pathlib import Path

import numpy as np
import pytest

from MatrixVectorMul_bitexact.wf_gemv.gemv import (
    adder_delays,
    binary_sum,
    dot,
    f32_bits,
    gemv,
)

ROOT = Path(__file__).resolve().parents[1]
SIZES = [(1, 16), (4, 64), (7, 128), (2, 48), (16, 32), (3, 176)]
DELAYS = 4


def _rows(path: Path) -> list[list[str]]:
    return [ln.split() for ln in path.read_text().splitlines()
            if ln.strip() and not ln.lstrip().startswith("#")]


def _load(m: int, n: int) -> dict:
    ni = _rows(ROOT / "data" / f"input_M{m}_N{n}.txt")
    n_case, mm, nn = (int(v) for v in ni[0])
    assert (mm, nn) == (m, n)
    vals = [int(v[0]) for v in ni[1:]]
    f32 = lambda u: struct.unpack("<f", struct.pack("<I", u))[0]
    cases, off = [], 0
    for _ in range(n_case):
        A = np.array([f32(u) for u in vals[off:off + m * n]], dtype=np.float32).reshape(m, n)
        off += m * n
        x = np.array([f32(u) for u in vals[off:off + n]], dtype=np.float32)
        off += n
        cases.append((A, x))
    g = _rows(ROOT / "golden" / f"gemv_f32_M{m}_N{n}_sweepP.txt")
    hdr = [int(v) for v in g[0]]
    n_logp = hdr[0]
    return {"cases": cases, "logps": hdr[1:1 + n_logp],
            "hw": [int(v[0]) for v in g[1:]], "M": m, "N": n}


@pytest.fixture(scope="module")
def loaded() -> dict:
    return {s: _load(*s) for s in SIZES}


def _naive_tree(a, b):
    """The plausible wrong model: one binary tree over all N products.

    Zero-padded to a power of two so it stays defined for a non-power-of-two N, which the library
    supports and the suite now covers (N=48, N=176).  Zero padding is exact in IEEE-754, so it
    introduces no rounding of its own.
    """
    v = [np.float32(a[i] * b[i]) for i in range(len(a))]
    while len(v) & (len(v) - 1):
        v.append(np.float32(0))
    while len(v) > 1:
        v = [np.float32(v[i] + v[i + 1]) for i in range(0, len(v), 2)]
    return v[0]


# --- structure --------------------------------------------------------------------------------
def test_adder_delays_matches_the_library():
    """``AdderDelay<T>`` -- a property of the element type, and it changes the answer."""
    assert (adder_delays("float32"), adder_delays("float64"), adder_delays("int32")) == (4, 8, 1)


def test_binary_sum_requires_power_of_two():
    """``BinarySum<T,N>`` halves until it reaches 1; other lengths would silently misgroup."""
    assert binary_sum([1.0, 2.0, 3.0, 4.0]) == np.float32(10.0)
    with pytest.raises(ValueError, match="power-of-two"):
        binary_sum([1.0, 2.0, 3.0])


def test_dot_rejects_a_bad_length():
    with pytest.raises(ValueError, match="multiple of parEntries"):
        dot(np.ones(6, dtype=np.float32), np.ones(6, dtype=np.float32), 4)


def test_goldens_are_the_expected_shape(loaded):
    """Guard the goldens -- a short regen would make every other test vacuous."""
    for (m, n), d in loaded.items():
        assert d["logps"] == [0, 1, 2, 3, 4]
        assert len(d["hw"]) == len(d["logps"]) * len(d["cases"]) * m


# --- the gate ---------------------------------------------------------------------------------
@pytest.mark.parametrize("size", SIZES, ids=[f"M{m}_N{n}" for m, n in SIZES])
def test_gemv_is_bit_exact(loaded, size):
    """THE GATE: the model equals xf::blas::gemv bit for bit, at every swept stream width."""
    d = loaded[size]
    m = d["M"]
    i = bad = 0
    for lp in d["logps"]:
        for A, x in d["cases"]:
            got = gemv(A, x, 1 << lp)
            for r in range(m):
                bad += f32_bits(got[r]) != d["hw"][i]
                i += 1
    assert bad == 0, f"M={size[0]} N={size[1]}: {bad} of {len(d['hw'])} rows differ"


# --- teeth ------------------------------------------------------------------------------------
def test_a_naive_tree_would_be_wrong(loaded):
    """A single binary tree over all N products is the obvious model, and it is wrong.

    The library trees *within* a beat and *within* a chunk of ``Delays`` beats, but accumulates
    *across* chunks sequentially.  If this stops failing, that chunked accumulation has been
    "simplified" into a plain tree.
    """
    d = loaded[(7, 128)]
    lp2 = d["logps"].index(2)
    base = lp2 * len(d["cases"]) * d["M"]
    bad = 0
    for k, (A, x) in enumerate(d["cases"]):
        for r in range(d["M"]):
            bad += f32_bits(_naive_tree(A[r], x)) != d["hw"][base + k * d["M"] + r]
    assert bad > 0, "the naive tree matched everywhere; the goldens no longer discriminate"


def test_numpy_dot_would_be_wrong(loaded):
    """``numpy.dot`` -- the first thing anyone reaches for -- disagrees.

    Worth pinning because numpy is *unreliably* right: it matched this kernel at N=64 with simple
    data while differing at N=16, so a small suite would have blessed it.
    """
    d = loaded[(7, 128)]
    lp2 = d["logps"].index(2)
    base = lp2 * len(d["cases"]) * d["M"]
    bad = 0
    for k, (A, x) in enumerate(d["cases"]):
        for r in range(d["M"]):
            bad += f32_bits(np.dot(A[r], x)) != d["hw"][base + k * d["M"] + r]
    assert bad > 0, "numpy.dot matched everywhere; the goldens no longer discriminate"


def test_stream_width_changes_the_result(loaded):
    """``t_LogParEntries`` is part of the numerical contract, not just a throughput knob.

    A design that retunes the stream width changes its output bits, so a model ignoring the
    parameter could not pass the gate above.
    """
    d = loaded[(7, 128)]
    A, x = d["cases"][-1]
    seen = {tuple(f32_bits(v) for v in gemv(A, x, 1 << lp)) for lp in d["logps"]}
    assert len(seen) > 1, "every stream width gave the same answer; the model ignores it"


def test_one_chunk_sizes_cannot_discriminate_and_that_is_recorded(loaded):
    """`M=1, N=16` is in the suite even though it *cannot* separate the two models.

    With ``N/P = 4`` beats and ``Delays = 4``, there is exactly one chunk, so the chunk tree and a
    full tree are the same reduction.  The size still pins the no-cross-chunk path, but a gate
    resting only on it would be vacuous -- which is why the larger sizes exist.  This asserts the
    property rather than leaving it as folklore.
    """
    d = loaded[(1, 16)]
    lp2 = d["logps"].index(2)
    base = lp2 * len(d["cases"]) * d["M"]
    same = all(f32_bits(_naive_tree(A[0], x)) == d["hw"][base + k]
               for k, (A, x) in enumerate(d["cases"]))
    assert same, "N=16 unexpectedly discriminates; the chunking assumption has changed"


def test_a_full_tree_and_the_library_coincide_below_four_chunks(loaded):
    """The exact condition under which the gate goes blind -- measured, not assumed.

    A zero-padded full binary tree and the library's reduction are the **same reduction** whenever
    the chunk count ``ceil((N/P) / Delays)`` is 3 or fewer, because a left-fold and a balanced
    tree agree up to three terms::

        k = 3   (c0 + c1) + c2          == ((0 + c0) + c1) + c2
        k = 4   (c0 + c1) + (c2 + c3)   != ((c0 + c1) + c2) + c3

    S2 recorded this as "one chunk", from ``M=1, N=16`` at ``P=4``.  That was too narrow: adding
    ``(2, 48)`` to the sweep, where ``P=4`` gives 12 beats but only 3 chunks, made the generator's
    old guard raise a false alarm.  Both halves are asserted here against the real goldens, so
    neither the rule nor its boundary can drift back into folklore.
    """
    delays = adder_delays("float32")
    blind = seeing = 0
    for (m, n), d in loaded.items():
        for lp in d["logps"]:
            par = 1 << lp
            if n % par:
                continue
            chunks = -(-(n // par) // delays)
            base = d["logps"].index(lp) * len(d["cases"]) * m
            same = all(f32_bits(_naive_tree(A[r], x)) == d["hw"][base + k * m + r]
                       for k, (A, x) in enumerate(d["cases"]) for r in range(m))
            if chunks < 4:
                assert same, (
                    f"M={m} N={n} P={par}: {chunks} chunks, so a full tree should be the same "
                    f"reduction, but the golden disagrees -- the chunking rule has changed")
                blind += 1
            else:
                seeing += not same
    assert blind, "no size/width combination in the suite has fewer than four chunks"
    assert seeing, (
        "no size/width combination with >= 4 chunks separates a full tree from the library; "
        "the whole suite has stopped discriminating")
