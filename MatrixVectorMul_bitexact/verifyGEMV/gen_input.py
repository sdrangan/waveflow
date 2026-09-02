#!/usr/bin/env python3
"""Generate the three input files this verification package feeds to Vitis.

Small on purpose -- co-simulation runs the synthesized RTL in xsim, so every extra case costs
real time.  What the data must NOT be is easy: at these sizes a dot product only reveals its
summation order when the partial sums differ in magnitude, so each set includes wide dynamic
range and cancellation rather than uniform random values.

Sizes match the DUT constants in ``src/gemv_top.hpp`` and the testbenches refuse to run if they
drift apart.
"""
from __future__ import annotations

import struct
from pathlib import Path

import numpy as np

M, N = 4, 64            # float DUTs   -- 16 beats at P=4 => 4 chunks, the smallest
FM, FN = 3, 32          # ap_fixed DUT    size at which the reduction is not a plain tree
FW, FI = 16, 8
HERE = Path(__file__).resolve().parent

#: The same scalar pairs the native S5 golden sweeps, so the two are directly comparable.
AB = [(1.0, 0.0), (0.0, 1.0), (2.5, -1.5), (0.1, 0.3), (1e-8, 1e8), (-3.25, 7.75)]


def _f32(a) -> np.ndarray:
    return np.asarray(a, dtype=np.float32)


def _bits(f) -> int:
    return struct.unpack("<I", struct.pack("<f", np.float32(f)))[0]


def _float_cases(rng, m: int, n: int, with_y: bool):
    out = []
    out.append((_f32(rng.random((m, n))), _f32(rng.random(n))))
    out.append((_f32(rng.standard_normal((m, n)) * np.float32(10.0) ** rng.integers(-6, 7, (m, n))),
                _f32(rng.standard_normal(n))))
    big = _f32(rng.standard_normal((m, n)) * 1e6)
    big[:, ::2] = -big[:, 1::2]                      # adjacent pairs cancel
    out.append((big, _f32(np.ones(n))))
    out.append((_f32(np.arange(1, m * n + 1)).reshape(m, n), _f32(1.0 / np.arange(1, n + 1))))
    if not with_y:
        return [(a, x, None) for a, x in out]
    ys = [_f32(rng.random(m)), _f32(rng.standard_normal(m) * 1e9),
          _f32(rng.standard_normal(m) * 1e-9), _f32(rng.standard_normal(m))]
    return [(a, x, y) for (a, x), y in zip(out, ys)]


def write_f32() -> None:
    cases = _float_cases(np.random.default_rng(20260902), M, N, with_y=False)
    lines = ["# verifyGEMV input -- float, 5-arg gemv.  IEEE-754 bit patterns.",
             "# per case: M*N matrix (row-major) then N vector entries",
             "# n_cases M N", f"{len(cases)} {M} {N}"]
    for a, x, _ in cases:
        lines += [str(_bits(v)) for v in a.reshape(-1)] + [str(_bits(v)) for v in x]
    (HERE / "data" / "input_f32.txt").write_text("\n".join(lines) + "\n")
    print(f"wrote data/input_f32.txt: {len(cases)} cases, M={M} N={N}")


def write_ab() -> None:
    cases = _float_cases(np.random.default_rng(20260903), M, N, with_y=True)
    lines = ["# verifyGEMV input -- alpha/beta gemv.  IEEE-754 bit patterns.",
             "# per case: M*N matrix, N vector, M y-entries; then n_ab (alpha, beta) pairs",
             "# n_cases M N n_ab", f"{len(cases)} {M} {N} {len(AB)}"]
    for a, x, y in cases:
        lines += ([str(_bits(v)) for v in a.reshape(-1)]
                  + [str(_bits(v)) for v in x] + [str(_bits(v)) for v in y])
    for al, be in AB:
        lines += [str(_bits(al)), str(_bits(be))]
    (HERE / "data" / "input_ab.txt").write_text("\n".join(lines) + "\n")
    print(f"wrote data/input_ab.txt: {len(cases)} cases x {len(AB)} (alpha,beta), M={M} N={N}")


def write_fixed() -> None:
    rng = np.random.default_rng(20260904)
    lo, hi = -(1 << (FW - 1)), (1 << (FW - 1)) - 1
    frac = FW - FI
    # Magnitude per ROW, so some rows overflow the accumulator and some do not -- overflow is a
    # property of the whole row, and per-element mixing makes every row overflow.
    mats, vecs = [], []
    for _ in range(3):
        e = rng.integers(-2, FI // 2 + 3, size=FM)[:, None]
        mag = np.clip((2.0 ** (e + frac)).astype(np.int64), 1, hi)
        a = rng.integers(lo, hi, size=(FM, FN)) % (mag + 1) * rng.choice([-1, 1], (FM, FN))
        mats.append(np.clip(a, lo, hi))
        mv = 1 << (frac + 2)
        vecs.append(rng.integers(lo, hi, size=FN) % (mv + 1) * rng.choice([-1, 1], FN))
    lines = [f"# verifyGEMV input -- ap_fixed<{FW},{FI}> gemv.  STORED INTEGERS, not decimals.",
             "# per case: M*N matrix (row-major) then N vector entries",
             "# W I n_cases M N", f"{FW} {FI} {len(mats)} {FM} {FN}"]
    for a, x in zip(mats, vecs):
        for row in a:
            lines.append(" ".join(str(int(v)) for v in row))
        lines.append(" ".join(str(int(v)) for v in x))
    (HERE / "data" / "input_fixed.txt").write_text("\n".join(lines) + "\n")
    print(f"wrote data/input_fixed.txt: {len(mats)} cases, ap_fixed<{FW},{FI}> M={FM} N={FN}")


if __name__ == "__main__":
    (HERE / "data").mkdir(exist_ok=True)
    write_f32()
    write_ab()
    write_fixed()
