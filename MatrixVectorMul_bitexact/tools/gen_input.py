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

M, N = 4, 64          # N/P = 16 at P=4, enough depth for the chunking to matter
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


def _cases() -> list[tuple[str, np.ndarray, np.ndarray]]:
    rng = np.random.default_rng(20260901)
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
    found = 0
    while found < 4:
        A = _f32(rng.standard_normal((M, N)) * np.float32(10.0) ** rng.integers(-4, 5, (M, N)))
        x = _f32(rng.standard_normal(N))
        if any(_bits(_library(A[r], x)) != _bits(_naive(A[r], x)) for r in range(M)):
            out.append((f"discriminating-{found}", A, x))
            found += 1
    return out


def main() -> None:
    cases = _cases()
    out = Path(__file__).resolve().parents[1] / "data" / "input.txt"
    out.parent.mkdir(exist_ok=True)
    lines = ["# Vitis BLAS gemv verification input",
             "# IEEE-754 bit patterns; M*N matrix entries (row-major) then N vector entries",
             "# cases: " + ", ".join(f"{i}={n}" for i, (n, _, _) in enumerate(cases)),
             "# n_cases M N",
             f"{len(cases)} {M} {N}"]
    for _, A, x in cases:
        lines += [str(_bits(v)) for v in A.reshape(-1)]
        lines += [str(_bits(v)) for v in x]
    out.write_text("\n".join(lines) + "\n")
    print(f"wrote {out}: {len(cases)} cases, M={M} N={N}")
    for i, (n, A, x) in enumerate(cases):
        d = sum(1 for r in range(M) if _bits(_library(A[r], x)) != _bits(_naive(A[r], x)))
        print(f"   case {i} {n:<20s} library vs naive-tree: {d}/{M} rows differ")


if __name__ == "__main__":
    main()
