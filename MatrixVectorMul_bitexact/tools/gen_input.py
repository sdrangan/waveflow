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
#:
#: The second three are the S5 m/n sweep, chosen for the beat/chunk arithmetic rather than for
#: size.  With B = N/P beats and Delays=4 beats per chunk, `padding()` only does something when
#: B is not a multiple of 4:
#:   (2, 48)   B = 3 at P=16 -> padded; B = 12 at P=4 -> exactly 3 chunks, no padding
#:   (16, 32)  B = 2 at P=16 -> padded; many rows against a short vector
#:   (3, 176)  B = 11 at P=16 -> padded to 12; B = 44 at P=4 -> 11 chunks, the longest reduction
#: So the same size pads at one stream width and not at another, which is the case a model that
#: hard-codes either behaviour gets wrong.
SIZES = [(1, 16), (4, 64), (7, 128), (2, 48), (16, 32), (3, 176)]
#: The stream widths the golden sweeps, and AdderDelay<float>::m_Delays.  The search below runs
#: at every width, because whether the two models can be told apart depends on the width -- see
#: `_can_discriminate`.
PS, DELAYS = (1, 2, 4, 8, 16), 4


def _f32(a) -> np.ndarray:
    return np.asarray(a, dtype=np.float32)


def _btree(v):
    """A plain full binary tree -- the plausible wrong model this file searches for data against.

    Zero-padded up to a power of two so it stays defined for a non-power-of-two N (the library
    handles those; a full tree over 48 products otherwise is not a well-formed reduction).
    Padding with zero is exact in IEEE-754 for every finite value, so it adds no rounding of its
    own and the comparison stays honest.
    """
    v = list(v)
    while len(v) & (len(v) - 1):
        v.append(np.float32(0))
    while len(v) > 1:
        v = [np.float32(v[i] + v[i + 1]) for i in range(0, len(v), 2)]
    return v[0]


def _library(a, b, par):
    """The structure this project models -- see wf_gemv/gemv.py."""
    prods = [np.float32(a[i] * b[i]) for i in range(len(a))]
    beats = [_btree(prods[s:s + par]) for s in range(0, len(prods), par)]
    while len(beats) % DELAYS:
        beats.append(np.float32(0))
    tot = np.float32(0)
    for s in range(0, len(beats), DELAYS):
        tot = np.float32(tot + _btree(beats[s:s + DELAYS]))
    return tot


def _chunks(n: int, par: int) -> int:
    """Number of ``Delays``-beat chunks the reduction accumulates across."""
    return -(-(n // par) // DELAYS)


def _can_discriminate(n: int, par: int) -> bool:
    """Whether a full tree and the library's reduction can differ at all, at this width.

    **Measured, across every (N, P) in the suite and several more: they are the same reduction
    whenever the chunk count is 3 or fewer, and differ often once it reaches 4.**

    The reason is that with fewer than four chunks the left-fold and the balanced tree coincide::

        k = 1   c0                      == 0 + c0
        k = 2   c0 + c1                 == (0 + c0) + c1
        k = 3   (c0 + c1) + c2          == ((0 + c0) + c1) + c2      <- padded slot contributes 0
        k = 4   (c0 + c1) + (c2 + c3)   != ((c0 + c1) + c2) + c3     <- first real difference

    This is a sharper statement than "one chunk", which is how it was first recorded in S2 from
    the ``M=1, N=16`` case.  It was found the hard way: adding ``(2, 48)`` to the sweep made the
    old guard -- ``N // P > DELAYS``, i.e. "more than one chunk" -- raise a false alarm at
    ``P = 4``, where there are 12 beats but only 3 chunks and no discriminating vector exists.
    """
    return _chunks(n, par) >= 4


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
    # Search at every swept width, not just one: the golden covers all five, and whether the two
    # models can be told apart is a property of the width as much as of the size.
    widths = [par for par in PS if N % par == 0 and _can_discriminate(N, par)]
    found, tries = 0, 0
    while found < 4 and tries < 20000 and widths:
        tries += 1
        A = _f32(rng.standard_normal((M, N)) * np.float32(10.0) ** rng.integers(-4, 5, (M, N)))
        x = _f32(rng.standard_normal(N))
        if any(_bits(_library(A[r], x, par)) != _bits(_naive(A[r], x))
               for par in widths for r in range(M)):
            out.append((f"discriminating-{found}", A, x))
            found += 1
    if found == 0 and widths:
        raise AssertionError(
            f"M={M} N={N}: no discriminating vector in {tries} tries, but widths {widths} each "
            f"give >= 4 chunks, so one should exist -- have the two models been made accidentally "
            "identical?")
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
        widths = [par for par in PS if N % par == 0]
        disc = sum(sum(1 for par in widths for r in range(M)
                       if _bits(_library(A[r], x, par)) != _bits(_naive(A[r], x)))
                   for _, A, x in cases)
        usable = [par for par in widths if _can_discriminate(N, par)]
        print(f"wrote {out.name}: {len(cases)} cases, M={M} N={N}, "
              f"{disc} (row, width) pairs where library != naive tree; "
              f"widths that can discriminate at all: {usable or 'none'}")


if __name__ == "__main__":
    main()
