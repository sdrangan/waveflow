#!/usr/bin/env python3
"""Generate the input files this verification package feeds to Vitis.

Small on purpose -- co-simulation runs the synthesized RTL in xsim, so every extra case costs
real time.  What the data must NOT be is easy: a dot product only reveals its summation order
when the partial sums differ in magnitude, so each set spans a wide dynamic range and includes
cancellation rather than uniform random values.

Sizes match the DUT constants in ``src/gemv_top.hpp``; the testbenches refuse to run if they
drift apart.  Every file uses the same header -- ``n_cases M N`` -- so one reader serves all.
"""
from __future__ import annotations

import struct
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent

#: name -> (M, N).  Must match src/gemv_top.hpp.
FLOAT_SIZES = {"f32": (4, 64), "f32_wide": (3, 128), "f32_pad": (2, 208)}
F64_SIZE = (2, 64)
INT_SIZE = (3, 32)          # shared by i32, u32, fixed and fix24

#: The same scalar pairs the native S5 golden sweeps, so the two are directly comparable.
AB = [(1.0, 0.0), (0.0, 1.0), (2.5, -1.5), (0.1, 0.3), (1e-8, 1e8), (-3.25, 7.75)]


def _b32(f) -> int:
    return struct.unpack("<I", struct.pack("<f", np.float32(f)))[0]


def _b64(d) -> int:
    return struct.unpack("<Q", struct.pack("<d", np.float64(d)))[0]


def _float_cases(rng, m: int, n: int, dtype):
    """Four shapes of data, each chosen to stress the reduction differently."""
    t = np.float32 if dtype is np.float32 else np.float64
    exp = 6 if dtype is np.float32 else 12
    out = [
        (rng.random((m, n)).astype(t), rng.random(n).astype(t)),
        ((rng.standard_normal((m, n)) * 10.0 ** rng.integers(-exp, exp + 1, (m, n))).astype(t),
         rng.standard_normal(n).astype(t)),
    ]
    big = (rng.standard_normal((m, n)) * 10.0 ** exp).astype(t)
    big[:, ::2] = -big[:, 1::2]                      # adjacent pairs cancel
    out.append((big, np.ones(n, dtype=t)))
    out.append((np.arange(1, m * n + 1, dtype=t).reshape(m, n),
                (1.0 / np.arange(1, n + 1)).astype(t)))
    return out


def _write(path: Path, header: str, m: int, n: int, blocks: list[list[int]]) -> None:
    lines = [f"# {header}", "# per case: M*N matrix (row-major) then N vector entries",
             "# n_cases M N", f"{len(blocks)} {m} {n}"]
    for b in blocks:
        lines += [str(v) for v in b]
    path.write_text("\n".join(lines) + "\n")
    print(f"wrote {path.name}: {len(blocks)} cases, M={m} N={n}")


def write_float(name: str, m: int, n: int, seed: int) -> None:
    cases = _float_cases(np.random.default_rng(seed), m, n, np.float32)
    _write(HERE / "data" / f"input_{name}.txt",
           "verifyGEMV input -- float.  IEEE-754 32-bit patterns, decimal.",
           m, n, [[_b32(v) for v in a.reshape(-1)] + [_b32(v) for v in x] for a, x in cases])


def write_f64() -> None:
    m, n = F64_SIZE
    cases = _float_cases(np.random.default_rng(20260905), m, n, np.float64)
    _write(HERE / "data" / "input_f64.txt",
           "verifyGEMV input -- double.  IEEE-754 64-bit patterns, decimal.",
           m, n, [[_b64(v) for v in a.reshape(-1)] + [_b64(v) for v in x] for a, x in cases])


def write_ab() -> None:
    m, n = FLOAT_SIZES["f32"]
    rng = np.random.default_rng(20260903)
    cases = _float_cases(rng, m, n, np.float32)
    ys = [rng.random(m).astype(np.float32),
          (rng.standard_normal(m) * 1e9).astype(np.float32),
          (rng.standard_normal(m) * 1e-9).astype(np.float32),
          rng.standard_normal(m).astype(np.float32)]
    lines = ["# verifyGEMV input -- alpha/beta gemv.  IEEE-754 32-bit patterns, decimal.",
             "# per case: M*N matrix, N vector, M y-entries; then n_ab (alpha, beta) pairs",
             "# n_cases M N n_ab", f"{len(cases)} {m} {n} {len(AB)}"]
    for (a, x), y in zip(cases, ys):
        lines += ([str(_b32(v)) for v in a.reshape(-1)] + [str(_b32(v)) for v in x]
                  + [str(_b32(v)) for v in y])
    for al, be in AB:
        lines += [str(_b32(al)), str(_b32(be))]
    (HERE / "data" / "input_ab.txt").write_text("\n".join(lines) + "\n")
    print(f"wrote input_ab.txt: {len(cases)} cases x {len(AB)} (alpha,beta), M={m} N={n}")


def _intlike_blocks(rng, w: int, i_bits: int | None, frac: int, top: int):
    """One attempt at three cases of stored integers.

    Magnitude is chosen PER ROW, not per element: whether the accumulator overflows is a property
    of the whole row, so per-element mixing makes every row behave the same and the wrapping
    stops being distinguishable.
    """
    m, n = INT_SIZE
    lo, hi = -(1 << (w - 1)), (1 << (w - 1)) - 1
    blocks = []
    for _ in range(3):
        e = rng.integers(-2, top, size=m)[:, None]
        mag = np.clip((2.0 ** (e + frac)).astype(np.int64), 1, hi)
        a = np.clip(rng.integers(lo, hi, size=(m, n)) % (mag + 1) * rng.choice([-1, 1], (m, n)),
                    lo, hi)
        mv = 1 << min(frac + 2, w - 2)
        x = rng.integers(lo, hi, size=n) % (mv + 1) * rng.choice([-1, 1], n)
        blocks.append((a, x))
    return blocks


def _wrap_count(blocks, w: int) -> tuple[int, int]:
    """How many rows overflow a ``w``-bit accumulator, and how many rows there are."""
    lim = 1 << (w - 1)
    wraps = rows = 0
    for a, x in blocks:
        for r in range(a.shape[0]):
            rows += 1
            wraps += abs(int(sum(int(p) * int(q) for p, q in zip(a[r], x)))) >= lim
    return wraps, rows


def write_intlike(name: str, w: int, i_bits: int | None, seed: int) -> None:
    """Stored integers for one of the dot_dsp DUTs, with the wrapping actually exercised.

    ``dot_dsp`` accumulates into the element type, so a long dot product wraps -- and wrapping is
    the behaviour a model is most likely to get wrong.  The first version of this data sat four
    orders of magnitude below the accumulator limit, so **not one row wrapped** and the DUT would
    have passed for a model with no wrapping at all.  This now searches for a scale at which some
    rows overflow and some do not, and raises rather than shipping data that proves nothing.
    """
    m, n = INT_SIZE
    base = (w // 2 - 5) if i_bits is None else w - i_bits
    top = 8 if i_bits is None else i_bits // 2 + 3
    # Search DOWN as well as up: too small a scale means nothing wraps, too large means
    # everything does, and both extremes make the DUT uninformative.  ap_fixed<24,12> needs a
    # smaller scale than its fraction length suggests, which an upward-only search never finds.
    best = None
    for bump in sorted(range(-8, 9), key=abs):
        rng = np.random.default_rng(seed + abs(bump))
        blocks = _intlike_blocks(rng, w, i_bits, base + bump, top)
        wraps, rows = _wrap_count(blocks, w)
        if best is None or abs(wraps - rows / 2) < abs(best[0] - best[1] / 2):
            best = (wraps, rows, base + bump)
        if 0 < wraps < rows:
            kind = f"ap_fixed<{w},{i_bits}>" if i_bits is not None else f"{w}-bit integer"
            _write(HERE / "data" / f"input_{name}.txt",
                   f"verifyGEMV input -- {kind}.  STORED INTEGERS, not decimals.  "
                   f"{wraps}/{rows} rows overflow the accumulator.",
                   m, n, [[int(v) for v in a.reshape(-1)] + [int(v) for v in x]
                          for a, x in blocks])
            return
    raise RuntimeError(
        f"{name}: no scale gave a mix of wrapping and non-wrapping rows "
        f"(closest was {best[0]}/{best[1]} at frac={best[2]}).  Widen the per-row exponent range "
        f"rather than accepting data where the accumulator always or never overflows.")


if __name__ == "__main__":
    (HERE / "data").mkdir(exist_ok=True)
    write_float("f32", *FLOAT_SIZES["f32"], seed=20260902)
    write_float("f32_wide", *FLOAT_SIZES["f32_wide"], seed=20260906)
    write_float("f32_pad", *FLOAT_SIZES["f32_pad"], seed=20260907)
    write_f64()
    write_ab()
    # i32 and u32 share one file on purpose: identical stored bits, two interpretations.  That is
    # exactly the distinction the unsigned DUT exists to check.
    write_intlike("int", 32, None, seed=20260908)
    write_intlike("fixed", 16, 8, seed=20260904)
    write_intlike("fix24", 24, 12, seed=20260909)
