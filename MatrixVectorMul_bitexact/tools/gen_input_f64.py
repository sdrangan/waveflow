#!/usr/bin/env python3
"""Generate the double-precision input for ``cpp/dump_gemv_f64.cpp``.

``double`` takes the same ``dot_tree`` path as ``float``, but ``AdderDelay<double>`` is **8**, so
beats are grouped by 8 rather than 4 and the reduction has a different shape.  The data therefore
needs enough beats to cross several groups, or the grouping never shows.

Same requirement as the float generator: a dot product only reveals its summation order when the
partial sums differ in magnitude, so this searches for vectors on which the library's structure
and a plain binary tree provably disagree, and raises rather than settling for weaker data.
"""
from __future__ import annotations

import struct
from pathlib import Path

import numpy as np

SIZES = [(3, 128)]      # 128/4 = 32 beats => 4 groups of 8: the smallest count that discriminates
DELAYS = 8              # AdderDelay<double>::m_Delays
PS = (2, 4, 8)          # the stream widths the dumper sweeps


def _bits(d) -> int:
    return struct.unpack("<Q", struct.pack("<d", np.float64(d)))[0]


def _btree(v):
    v = list(v)
    while len(v) & (len(v) - 1):
        v.append(np.float64(0))
    while len(v) > 1:
        v = [np.float64(v[i] + v[i + 1]) for i in range(0, len(v), 2)]
    return v[0]


def _library(a, b, par):
    prods = [np.float64(a[i] * b[i]) for i in range(len(a))]
    beats = [_btree(prods[s:s + par]) for s in range(0, len(prods), par)]
    while len(beats) % DELAYS:
        beats.append(np.float64(0))
    tot = np.float64(0)
    for s in range(0, len(beats), DELAYS):
        tot = np.float64(tot + _btree(beats[s:s + DELAYS]))
    return tot


def _cases(m: int, n: int):
    rng = np.random.default_rng(20260902 + m * 1000 + n)
    out = [("uniform-random", rng.random((m, n)), rng.random(n)),
           ("wide-dynamic-range",
            rng.standard_normal((m, n)) * 10.0 ** rng.integers(-12, 13, (m, n)),
            rng.standard_normal(n))]
    big = rng.standard_normal((m, n)) * 1e12
    big[:, ::2] = -big[:, 1::2]
    out.append(("cancellation", big, np.ones(n)))

    widths = [p for p in PS if n % p == 0 and -(-(n // p) // DELAYS) >= 4]
    found, tries = 0, 0
    while found < 3 and tries < 20000 and widths:
        tries += 1
        a = rng.standard_normal((m, n)) * 10.0 ** rng.integers(-8, 9, (m, n))
        x = rng.standard_normal(n)
        if any(_bits(_library(a[r], x, p)) != _bits(_btree([np.float64(a[r][i] * x[i])
                                                            for i in range(n)]))
               for p in widths for r in range(m)):
            out.append((f"discriminating-{found}", a, x))
            found += 1
    if found == 0 and widths:
        raise RuntimeError(
            f"M={m} N={n}: no vector separates the library's reduction from a full tree in "
            f"{tries} tries, though widths {widths} each give >= 4 groups of {DELAYS} beats. "
            f"Enlarge N rather than lowering the bar.")
    return out


def main() -> None:
    root = Path(__file__).resolve().parents[1] / "data"
    root.mkdir(exist_ok=True)
    for m, n in SIZES:
        cases = _cases(m, n)
        lines = ["# Vitis BLAS gemv double input -- IEEE-754 64-bit patterns (decimal)",
                 "# per case: M*N matrix (row-major) then N vector entries",
                 "# cases: " + ", ".join(f"{i}={nm}" for i, (nm, _, _) in enumerate(cases)),
                 "# n_cases M N", f"{len(cases)} {m} {n}"]
        for _, a, x in cases:
            lines += [str(_bits(v)) for v in np.asarray(a).reshape(-1)]
            lines += [str(_bits(v)) for v in np.asarray(x)]
        out = root / f"input_f64_M{m}_N{n}.txt"
        out.write_text("\n".join(lines) + "\n")
        print(f"wrote {out.name}: {len(cases)} cases, M={m} N={n}")


if __name__ == "__main__":
    main()
