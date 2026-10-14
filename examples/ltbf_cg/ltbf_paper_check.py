"""ltbf_paper_check.py -- AC2 (c): the float pipeline against the LTBF paper's own numbers.

``plans/mimo_cg/ltbf_cg_dse.md`` step 2.2.  On the first traced drops, regime 1, the factored float
CG, noiseless and nominal SRS, both bands, every array: per served UE the gap between
instantaneous MMSE and exact LTBF (the paper: within about 0.1 dB with noiseless SRS) and the CG
loss ``exact - CG(t)`` (the paper: t = 3 suffices).  Medians over the served UEs of all the drops,
per configuration, to ``results/paper_check.csv`` (local).

Run from the repo root::

    python -m examples.ltbf_cg.ltbf_paper_check --drops 1 2 3
"""

from __future__ import annotations

import argparse
import csv
import time
from pathlib import Path

import numpy as np

from examples.ltbf_cg import ltbf as lt
from examples.ltbf_cg import ltbf_link as lk
from examples.ltbf_cg import scenario as sc

RESULTS = Path(__file__).resolve().parent / "results"
T_SHOW = (1, 2, 3, 5, 10)
BANDS = ("1.44MHz", "100MHz")


def check(drops, arrays, bands, target_name, ranks=(1, 2)):
    rows = []
    target = lk.TARGETS[target_name]
    for band in bands:
        for n in arrays:
            for srs in (None, lk.G_SRS_DB):
                gap = {r: [] for r in ranks}
                loss = {(t, r): [] for t in T_SHOW for r in ranks}
                t0 = time.time()
                for d in drops:
                    drop = sc.load_drop(d)
                    rng = np.random.default_rng([d, n, BANDS.index(band), srs is None])
                    ev = lt.evaluate_drop(drop, n, band, target, srs, rng, ranks=ranks)
                    for r in ranks:
                        for u, v in ev.exact[r].items():
                            gap[r].append(ev.ins[u] - v)
                            for t in T_SHOW:
                                loss[(t, r)].append(
                                    v - ev.approx[("factored", 1, t, r)][u]
                                )
                for r in ranks:
                    row = {
                        "band": band, "n": n, "srs": "noiseless" if srs is None else f"{srs:g}dB",
                        "target": target_name, "r": r, "ues": len(gap[r]),
                        "median_mmse_minus_exact_db": float(np.median(gap[r])),
                    }  # fmt: skip
                    for t in T_SHOW:
                        row[f"median_loss_t{t}_db"] = float(np.median(loss[(t, r)]))
                        row[f"p90_loss_t{t}_db"] = float(
                            np.percentile(loss[(t, r)], 90)
                        )
                    row["seconds"] = round(time.time() - t0, 1)
                    rows.append(row)
                    print(
                        f"{band:8s} {n:2d}x{n:<2d} {row['srs']:9s} r={r} UEs={row['ues']:3d} "
                        f"MMSE-exact {row['median_mmse_minus_exact_db']:+.3f} dB; median loss "
                        + " ".join(
                            f"t{t} {row[f'median_loss_t{t}_db']:+.3f}" for t in T_SHOW
                        )
                        + f"  ({row['seconds']} s)",
                        flush=True,
                    )
    return rows


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--drops", type=int, nargs="+", default=[1, 2, 3])
    p.add_argument("--arrays", type=int, nargs="+", default=[8, 16, 32])
    p.add_argument("--bands", nargs="+", default=list(BANDS), choices=BANDS)
    p.add_argument("--target", default="-6_3", choices=sorted(lk.TARGETS))
    p.add_argument("--out", default=str(RESULTS / "paper_check.csv"))
    a = p.parse_args()
    rows = check(a.drops, a.arrays, a.bands, a.target)
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
