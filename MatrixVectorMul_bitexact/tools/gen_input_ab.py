#!/usr/bin/env python3
"""Generate the S5 (alpha/beta overload) inputs.

The overload is ``yr = alpha * (M x) + beta * y``.  Everything before the ``alpha *`` is already
pinned by S1-S2, so this data has one job: make the *combining* step discriminating.

Two things have to be visible in the output.

**FMA contraction.**  ``axpy`` computes ``p_alpha * l_realX + l_realY`` in a single expression.  A
fused multiply-add keeps the product's full precision; a separate multiply and add rounds it
first.  The (alpha, beta) pairs below include magnitudes far enough apart that the difference
lands in the result, so a golden built with contraction on would not match one built with it off.

**The scal/axpy split.**  ``beta * y`` happens in ``scal`` and is rounded to float32 *before*
``axpy`` adds it.  A model that computes ``alpha*dot + beta*y`` in double and rounds once at the
end gets different bits.
"""
from __future__ import annotations

import struct
from pathlib import Path

import numpy as np

#: (M, N) -- N must be a multiple of the widest swept parEntries (16).
SIZES = [(4, 64), (7, 128)]

#: (alpha, beta).  Chosen, not random:
#:   (1, 0)      must reproduce the 5-arg overload exactly -- a cross-check on the composition
#:   (0, 1)      must reproduce y exactly -- pins that beta*y is not disturbed
#:   (2.5, -1.5) exactly representable, so any difference is structural rather than rounding
#:   (0.1, 0.3)  not representable in binary; the multiplies round
#:   (1e-8, 1e8) the two terms differ by ~16 orders of magnitude, so the add discards most of one
#:               -- this is the pair an FMA and a mul-then-add disagree on
AB = [(1.0, 0.0), (0.0, 1.0), (2.5, -1.5), (0.1, 0.3), (1e-8, 1e8), (-3.25, 7.75)]


def _f32(a) -> np.ndarray:
    return np.asarray(a, dtype=np.float32)


def _bits(f) -> int:
    return struct.unpack("<I", struct.pack("<f", np.float32(f)))[0]


def _cases(m: int, n: int):
    rng = np.random.default_rng(20260902 + m * 1000 + n)
    out = []
    out.append(("uniform-random", _f32(rng.random((m, n))), _f32(rng.random(n)),
                _f32(rng.random(m))))
    out.append(("wide-dynamic-range",
                _f32(rng.standard_normal((m, n)) * np.float32(10.0) ** rng.integers(-6, 7, (m, n))),
                _f32(rng.standard_normal(n)),
                _f32(rng.standard_normal(m) * np.float32(10.0) ** rng.integers(-6, 7, m))))
    # y far larger than the dot product, so `alpha*dot + beta*y` is a catastrophic-cancellation
    # candidate and the order of rounding shows.
    out.append(("y-dominant", _f32(rng.standard_normal((m, n))), _f32(rng.standard_normal(n)),
                _f32(rng.standard_normal(m) * 1e9)))
    out.append(("y-negligible", _f32(rng.standard_normal((m, n)) * 1e6), _f32(rng.standard_normal(n)),
                _f32(rng.standard_normal(m) * 1e-9)))
    return out


def main() -> None:
    root = Path(__file__).resolve().parents[1] / "data"
    root.mkdir(exist_ok=True)
    for m, n in SIZES:
        cases = _cases(m, n)
        lines = ["# Vitis BLAS gemv alpha/beta input -- IEEE-754 bit patterns",
                 "# per case: M*N matrix (row-major), N vector, M y-entries",
                 "# then n_ab (alpha, beta) pairs",
                 "# cases: " + ", ".join(f"{i}={nm}" for i, (nm, _, _, _) in enumerate(cases)),
                 "# n_cases M N n_ab",
                 f"{len(cases)} {m} {n} {len(AB)}"]
        for _, a, x, y in cases:
            lines += [str(_bits(v)) for v in a.reshape(-1)]
            lines += [str(_bits(v)) for v in x]
            lines += [str(_bits(v)) for v in y]
        for al, be in AB:
            lines += [str(_bits(al)), str(_bits(be))]
        out = root / f"input_ab_M{m}_N{n}.txt"
        out.write_text("\n".join(lines) + "\n")
        print(f"wrote {out.name}: {len(cases)} cases x {len(AB)} (alpha,beta), M={m} N={n}")


if __name__ == "__main__":
    main()
