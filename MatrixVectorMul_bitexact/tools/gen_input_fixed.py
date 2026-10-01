#!/usr/bin/env python3
"""Generate the S4 (``ap_fixed``) input vectors.

Random data is not enough here.  The golden has to be able to *fail* for each thing the stage
claims, so this searches for stored-integer data on which

  * ``AP_TRN`` and ``AP_RND`` disagree on some row -- otherwise the Q sweep is decoration;
  * ``AP_WRAP`` and ``AP_SAT`` disagree on some row (the accumulator must actually overflow) --
    but not on every row, or the saturating configs would carry no information beyond "clipped";
  * the shipped kernel and the corrected one disagree -- the defect must be visible in the data.

It raises rather than settling for weaker data, so a size that stops discriminating announces
itself instead of quietly blessing a wrong model.  Same lesson as ``gen_input.py``: at
``M=1, N=16`` on the float path a full tree and the library's reduction are the *same* reduction,
and a suite resting on that case is vacuous.
"""
from __future__ import annotations

import pathlib
import sys

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from MatrixVectorMul_bitexact.wf_gemv.fixed import (
    OMode,
    QMode,
    fixed_format,
    gemv_fixed,
    gemv_fixed_as_shipped,
)

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _variants(w: int, i: int):
    return {
        "trn_wrap": fixed_format(w, i, QMode.AP_TRN, OMode.AP_WRAP),
        "rnd_wrap": fixed_format(w, i, QMode.AP_RND, OMode.AP_WRAP),
        "trn_sat": fixed_format(w, i, QMode.AP_TRN, OMode.AP_SAT),
        "rnd_sat": fixed_format(w, i, QMode.AP_RND, OMode.AP_SAT),
    }


def _score(mats, vecs, w: int, i: int) -> dict[str, int]:
    """Count the rows on which each pair of behaviours disagrees."""
    v = _variants(w, i)
    out = dict.fromkeys(("q", "o", "defect", "rows"), 0)
    for a, x in zip(mats, vecs):
        trn_w = gemv_fixed(a, x, v["trn_wrap"])
        rnd_w = gemv_fixed(a, x, v["rnd_wrap"])
        trn_s = gemv_fixed(a, x, v["trn_sat"])
        out["rows"] += len(trn_w)
        out["q"] += int(np.sum(trn_w != rnd_w))
        out["o"] += int(np.sum(trn_w != trn_s))
        out["defect"] += int(np.sum(trn_w != gemv_fixed_as_shipped(a, x, v["trn_wrap"])))
    return out


def search(w: int, i: int, m: int, n: int, n_case: int, seed0: int = 0, tries: int = 400):
    """Find data meeting every discrimination requirement, or raise saying which one failed."""
    lo, hi = -(1 << (w - 1)), (1 << (w - 1)) - 1
    best: tuple[dict, list, list] | None = None
    for seed in range(seed0, seed0 + tries):
        rng = np.random.default_rng(seed)
        # Magnitude is chosen PER ROW, not per element.  Whether the accumulator overflows is a
        # property of the whole row, so per-element mixing makes every row overflow and the
        # O-mode sweep stops discriminating -- which is exactly what the first attempt did.
        # Every row keeps full-range low bits so the Q-mode always has something to round.
        f = w - i
        mats, vecs = [], []
        for _ in range(n_case):
            # ~2**e per element => row sum ~ n * 2**(2e); overflow at 2**(i-1).
            e = rng.integers(-2, i // 2 + 3, size=m)[:, None]
            mag = np.clip((2.0 ** (e + f)).astype(np.int64), 1, hi)
            a = rng.integers(lo, hi, size=(m, n)) % (mag + 1) * rng.choice([-1, 1], (m, n))
            mats.append(np.clip(a, lo, hi))
            mv = 1 << (f + 2)
            vecs.append(rng.integers(lo, hi, size=n) % (mv + 1) * rng.choice([-1, 1], n))
        sc = _score(mats, vecs, w, i)
        ok = sc["q"] > 0 and 0 < sc["o"] < sc["rows"] and sc["defect"] > 0
        if ok:
            return sc, mats, vecs
        if best is None or (sc["q"] > 0) + (0 < sc["o"] < sc["rows"]) > \
                (best[0]["q"] > 0) + (0 < best[0]["o"] < best[0]["rows"]):
            best = (sc, mats, vecs)
    raise RuntimeError(
        f"no discriminating data for ap_fixed<{w},{i}> at M={m} N={n} in {tries} tries; "
        f"best was {best[0] if best else None}.  Q-mode needs fractional bits below the "
        f"accumulator LSB, O-mode needs SOME rows to overflow and some not to -- enlarge N or "
        f"widen the magnitude spread rather than lowering the bar.")


def write(path: pathlib.Path, w: int, i: int, m: int, n: int, mats, vecs, sc) -> None:
    with path.open("w") as f:
        f.write("# S4 ap_fixed gemv input -- STORED INTEGERS (the .range() field), not floats.\n")
        f.write(f"# ap_fixed<{w},{i}>, M={m}, N={n}, {len(mats)} cases\n")
        f.write(f"# discriminating rows: Q-mode {sc['q']}, O-mode {sc['o']}/{sc['rows']}, "
                f"defect {sc['defect']}\n")
        f.write("# header: W I n_case M N\n")
        f.write(f"{w} {i} {len(mats)} {m} {n}\n")
        for a, x in zip(mats, vecs):
            for row in a:
                f.write(" ".join(str(int(v)) for v in row) + "\n")
            f.write(" ".join(str(int(v)) for v in x) + "\n")
    print(f"wrote {path}  ({sc})")


def main() -> None:
    (ROOT / "data").mkdir(exist_ok=True)
    for w, i, m, n, n_case in ((16, 8, 3, 32, 4), (24, 12, 4, 16, 3)):
        sc, mats, vecs = search(w, i, m, n, n_case)
        write(ROOT / "data" / f"input_fixed_W{w}_M{m}_N{n}.txt", w, i, m, n, mats, vecs, sc)


if __name__ == "__main__":
    main()
