#!/usr/bin/env python3
"""Generate data/input.txt for the gemv golden.

Values are written as IEEE-754 bit patterns so the C++ and Python sides start from bit-identical
inputs -- no decimal parsing in the loop.

The case selection is the point.  A dot product's summation order only shows up when the partial
sums have *different magnitudes*, so uniform data hides the very thing this project models: at
M=4, N=16 an early experiment matched three different summation orders at once.  These cases
deliberately include wide dynamic range, cancellation, and vectors on which the library's
structure and a naive full tree are known to disagree.
"""
from __future__ import annotations

import struct
from pathlib import Path

import numpy as np

#: (M, N) pairs.  N must be a multiple of the widest swept parEntries (16).  The sizes probe
#: different corners: N=16 is one chunk of beats at P=4 (no cross-chunk accumulation at all),
#: N=64 exercises several chunks, and N=128 with M=7 gives a non-power-of-two row count.
SIZES = [(1, 16), (4, 64), (7, 128)]
P, DELAYS = 4, 4      # 1 << logParEntries ; AdderDelay<float>::m_Delays


def _f32(a) -> np.ndarray:
    return np.asarray(a, dtype=np.float32)


def _btree(v):
    v = list(v)
    while len(v) > 1:
        v = [np.float32(v[i] + v[i + 1]) for i in range(0, len(v), 2)]
    return v[0]


def _library(a, b):
    """The structure this project models -- see wf_gemv/gemv.py."""
    prods = [np.float32(a[i] * b[i]) for i in range(len(a))]
    beats = [_btree(prods[s:s + P]) for s in range(0, len(prods), P)]
    while len(beats) % DELAYS:
        beats.append(np.float32(0))
    tot = np.float32(0)
    for s in range(0, len(beats), DELAYS):
        tot = np.float32(tot + _btree(beats[s:s + DELAYS]))
    return tot


def _naive(a, b):
    return _btree([np.float32(a[i] * b[i]) for i in range(len(a))])


def _bits(f) -> int:
    return struct.unpack("<I", struct.pack("<f", np.float32(f)))[0]


def _cases(M: int, N: int) -> list[tuple[str, np.ndarray, np.ndarray]]:
    rng = np.random.default_rng(20260901 + M * 1000 + N)
    out: list[tuple[str, np.ndarray, np.ndarray]] = []

    out.append(("ramp", _f32(np.arange(1, M * N + 1)).reshape(M, N), _f32(1.0 / np.arange(1, N + 1))))
    out.append(("uniform-random", _f32(rng.random((M, N))), _f32(rng.random(N))))
    out.append(("wide-dynamic-range",
                _f32(rng.standard_normal((M, N)) * np.float32(10.0) ** rng.integers(-6, 7, (M, N))),
                _f32(rng.standard_normal(N))))
    big = _f32(rng.standard_normal((M, N)) * 1e6)
    big[:, ::2] = -big[:, 1::2]                      # adjacent pairs cancel
    out.append(("cancellation", big, _f32(np.ones(N))))

    # Vectors where the library structure and a naive full tree provably disagree.  Without these
    # the gate cannot tell the two apart, which is the one distinction that matters here.
    # Bounded, because at some sizes no such vector exists.  With N/P beats and Delays beats per
    # chunk, a single chunk (N <= P*Delays -- e.g. N=16 at P=4) makes the chunk tree and the full
    # tree the *same* reduction, so the two models are identical by construction and no search can
    # separate them.  That is a fact about the size, not a failure; an earlier unbounded loop
    # simply hung there.  Such sizes still earn their place -- they pin the no-cross-chunk path.
    found, tries = 0, 0
    while found < 4 and tries < 20000:
        tries += 1
        A = _f32(rng.standard_normal((M, N)) * np.float32(10.0) ** rng.integers(-4, 5, (M, N)))
        x = _f32(rng.standard_normal(N))
        if any(_bits(_library(A[r], x)) != _bits(_naive(A[r], x)) for r in range(M)):
            out.append((f"discriminating-{found}", A, x))
            found += 1
    if found == 0 and (N // P) > DELAYS:
        raise AssertionError(
            f"M={M} N={N}: no discriminating vector in {tries} tries, but {N // P} beats with "
            f"Delays={DELAYS} means more than one chunk, so one should exist -- have the two "
            "models been made accidentally identical?")
    return out


def main() -> None:
    root = Path(__file__).resolve().parents[1] / "data"
    root.mkdir(exist_ok=True)
    for M, N in SIZES:
        cases = _cases(M, N)
        out = root / f"input_M{M}_N{N}.txt"
        lines = ["# Vitis BLAS gemv verification input",
                 "# IEEE-754 bit patterns; M*N matrix entries (row-major) then N vector entries",
                 "# cases: " + ", ".join(f"{i}={n}" for i, (n, _, _) in enumerate(cases)),
                 "# n_cases M N",
                 f"{len(cases)} {M} {N}"]
        for _, A, x in cases:
            lines += [str(_bits(v)) for v in A.reshape(-1)]
            lines += [str(_bits(v)) for v in x]
        out.write_text("\n".join(lines) + "\n")
        disc = sum(sum(1 for r in range(M)
                       if _bits(_library(A[r], x)) != _bits(_naive(A[r], x)))
                   for _, A, x in cases)
        print(f"wrote {out.name}: {len(cases)} cases, M={M} N={N}, "
              f"{disc} rows where library != naive tree")


if __name__ == "__main__":
    main()
