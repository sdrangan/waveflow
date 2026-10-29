"""ltbf_paper_check.py -- the paper check of ``plans/ltbf_dse/ltbf_dse.md`` step 2.4 (AC5 c).

``paper/cg_dse``'s check (its AC2 c) measured, with the paper's model of SRS (the true channel plus
noise at 10 dB), the median gap between instantaneous MMSE and exact LTBF: 2.38 / 6.27 / 10.71 dB at
8x8 / 16x16 / 32x32 (100 MHz, target [-6, 3]; the paper 2.25 / 6.30 / 10.58).  Here the SRS is
rendered and estimated (spec §2, G_SRS 16.14 dB) and the receiver is realizable, so per served UE:

* ``mmse - exact (genie)``: the same gap with the genie SINR of this chain's exact ``G`` -- the like-
  for-like comparison;
* ``mmse - exact (measured)``: against the realizable receiver's measured SINR (DMRS estimates,
  whitened projection, MMSE weights);
* ``loss t3``: exact minus CG at t = 3 (factored, regime 1), genie and measured.

Medians over the served UEs of every drop, to ``results/paper_check.csv`` (local).  Not a gate.

    python -m examples.ltbf_dse.ltbf_paper_check --drops 1 2 3 4 5 6 7 8 9 10
"""

from __future__ import annotations

import argparse
import csv
import time
from pathlib import Path

import numpy as np

from examples.ltbf_dse import ltbf as lt
from examples.ltbf_dse import ltbf_chain as lc
from examples.ltbf_dse import ltbf_link as lk
from examples.ltbf_dse import scenario as sc

RESULTS = Path(__file__).resolve().parent / "results"


def check(drops, arrays, target_name, r=1):
    rows = []
    target = lk.TARGETS[target_name]
    for n in arrays:
        t0 = time.time()
        acc = {
            k: []
            for k in (
                "gap_genie",
                "gap_meas",
                "loss3_genie",
                "loss3_meas",
                "meas_minus_genie",
            )
        }
        for d in drops:
            res = lc.run_chain(d, n, target, ranks=(r,), t_max=3, t_eval=(3,))
            drop = sc.load_drop(d)
            h1 = lk.channel(drop, 1, n, lc.BAND)
            pc1 = lk.power_control(h1, target)
            ins = lt.sinr_ins_db(lk.scaled(h1, pc1), pc1, res.served)
            ge, gc = res.genie[("exact", r)], res.genie[("factored", 1, 3, r)]
            me, mc = res.metrics[("exact", r)], res.metrics[("factored", 1, 3, r)]
            for u in res.served:
                u = int(u)
                acc["gap_genie"].append(ins[u] - ge[u])
                acc["gap_meas"].append(ins[u] - me[u]["sinr_mmse_db"])
                acc["loss3_genie"].append(ge[u] - gc[u])
                acc["loss3_meas"].append(me[u]["sinr_mmse_db"] - mc[u]["sinr_mmse_db"])
                acc["meas_minus_genie"].append(me[u]["sinr_mmse_db"] - ge[u])
        row = {"n": n, "target": target_name, "r": r, "ues": len(acc["gap_genie"])}
        row.update(
            {f"median_{k}_db": round(float(np.median(v)), 3) for k, v in acc.items()}
        )
        row["seconds"] = round(time.time() - t0, 1)
        rows.append(row)
        print(row, flush=True)
    return rows


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--drops", type=int, nargs="+", default=list(range(1, 11)))
    p.add_argument("--arrays", type=int, nargs="+", default=[8, 16, 32])
    p.add_argument("--target", default="-6_3", choices=sorted(lk.TARGETS))
    p.add_argument("--out", default=str(RESULTS / "paper_check.csv"))
    a = p.parse_args()
    rows = check(a.drops, a.arrays, a.target)
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
