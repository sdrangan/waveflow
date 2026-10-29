"""ltbf_accuracy_sweep.py -- the accuracy DSE, no Vitis in the loop (plan step 3.2, AC7).

Per (drop, array, target) **job**: render and estimate once (``ltbf_chain.prepare``), then the exact
float LTBF (the reference), float CG and the bit-exact hardware chain (``ltbf_fixed_chain``) for every
fixed-point configuration, every ``t`` in :data:`T_EVAL` and ``r`` 1 and 2.  A job writes its per-UE
measured SINRs to ``paper_data/acc/jobs/<array>_<target>_<drop>.json`` (local) and is skipped when that
file exists (resumable).  ``merge`` turns the jobs into ``paper_data/accuracy_losses.csv``: per
(array, target, config, t, r) the median, 10th and 90th percentile and mean of the loss (exact minus
config, dB) over the served UEs of the evaluation drops.

Formats come from ``profile`` on the **profile drops** (1-10) per (array, target); the evaluation drops
(11-100) never set a format.  Every draw is seeded; workers run single-threaded BLAS, so a job's file is
byte for byte the same on a re-run.

    python -m examples.ltbf_dse.ltbf_accuracy_sweep profile
    python -m examples.ltbf_dse.ltbf_accuracy_sweep pilot
    python -m examples.ltbf_dse.ltbf_accuracy_sweep run --workers 4
    python -m examples.ltbf_dse.ltbf_accuracy_sweep merge
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
OUT = HERE / "paper_data" / "acc"
JOBS = OUT / "jobs"
PROFILE_DROPS = tuple(range(1, 11))
EVAL_DROPS = tuple(range(11, 101))
ARRAYS = (8, 16, 32)
TARGETS = ("-6_3", "-6_14")
#: Operand widths (16-bit lanes up to 16, 32-bit lanes above), guard bits, forms.
WIDTHS = (10, 12, 14, 16, 20)
GUARDS = (1,)
FORMS = ("factored", "explicit")
T_EVAL = (1, 2, 3, 5, 7, 10)
RANKS = (1, 2)


def _threads1() -> None:
    for v in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
        os.environ[v] = "1"


def profile_path(n: int, target: str) -> Path:
    return OUT / "profiles" / f"{n}x{n}_{target}.json"


def profile(n: int, target: str) -> dict:
    from examples.ltbf_dse import ltbf_chain as lc
    from examples.ltbf_dse import ltbf_fixed_chain as fc
    from examples.ltbf_dse import ltbf_link as lk

    rs = []
    for d in PROFILE_DROPS:
        prep = lc.prepare(d, n, lk.TARGETS[target])
        rs.append(fc.profile(prep, t_max=max(T_EVAL), ranks=RANKS))
    m = fc.merge(*rs)
    path = profile_path(n, target)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(m, indent=1, sort_keys=True))
    return m


def configs(ranges: dict) -> list:
    from examples.ltbf_dse import ltbf_fixed_chain as fc

    return [fc.formats(ranges, w, g, f) for f in FORMS for w in WIDTHS for g in GUARDS]


def job_path(n: int, target: str, drop: int) -> Path:
    return JOBS / f"{n}x{n}_{target}_{drop:03d}.json"


def run_job(args) -> str:
    n, target, drop = args
    path = job_path(n, target, drop)
    if path.exists():
        return f"skip {path.name}"
    _threads1()
    from examples.ltbf_dse import ltbf as lt
    from examples.ltbf_dse import ltbf_chain as lc
    from examples.ltbf_dse import ltbf_fixed_chain as fc
    from examples.ltbf_dse import ltbf_link as lk

    t0 = time.time()
    ranges = json.loads(profile_path(n, target).read_text())
    prep = lc.prepare(drop, n, lk.TARGETS[target])
    out: dict = {}
    exact, cg = {}, {}
    for s in range(len(prep.ltin.own)):
        exact.update(lt.sector_a(prep.ltin, s, lt.Solve(), None))
        cg.update(lt.sector_a(prep.ltin, s, lt.Solve("factored", 1), max(T_EVAL)))
    for r in RANKS:
        _g, gp = lc.project(prep, exact, r)
        out[f"exact|{r}"] = {
            u: m["sinr_mmse_db"] for u, m in lc.real_time(prep, gp).items()
        }
        for t in T_EVAL:
            _g, gp = lc.project(prep, {u: a[t - 1] for u, a in cg.items()}, r)
            out[f"float|{t}|{r}"] = {
                u: m["sinr_mmse_db"] for u, m in lc.real_time(prep, gp).items()
            }
    res = fc.run_fixed(
        prep, configs(ranges), t_max=max(T_EVAL), t_eval=T_EVAL, ranks=RANKS
    )
    for (name, t, r), per_ue in res.items():
        out[f"{name}|{t}|{r}"] = {u: m["sinr_mmse_db"] for u, m in per_ue.items()}
    path.parent.mkdir(parents=True, exist_ok=True)
    blob = {
        k: {str(u): round(float(v), 6) for u, v in sorted(d.items())}
        for k, d in sorted(out.items())
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(blob, sort_keys=True))
    tmp.replace(path)
    return f"done {path.name} in {time.time() - t0:.1f} s"


def all_jobs(drops=EVAL_DROPS, arrays=ARRAYS, targets=TARGETS) -> list:
    return [(n, tg, d) for n in arrays for tg in targets for d in drops]


def merge(drops=EVAL_DROPS, out_csv: Path | None = None) -> Path:
    rows = []
    for n in ARRAYS:
        for tg in TARGETS:
            files = [job_path(n, tg, d) for d in drops if job_path(n, tg, d).exists()]
            if not files:
                continue
            losses: dict = {}
            for f in files:
                blob = json.loads(f.read_text())
                for key, per_ue in blob.items():
                    if key.startswith("exact"):
                        continue
                    r = key.split("|")[-1]
                    ref = blob[f"exact|{r}"]
                    losses.setdefault(key, []).extend(
                        ref[u] - v for u, v in per_ue.items()
                    )
            for key in sorted(losses):
                cfg, t, r = key.split("|")
                v = np.array(losses[key])
                rows.append({
                    "array": f"{n}x{n}", "target": tg, "config": cfg, "t": int(t), "r": int(r),
                    "drops": len(files), "ues": v.size,
                    "median_loss_db": round(float(np.median(v)), 4),
                    "p10_loss_db": round(float(np.percentile(v, 10)), 4),
                    "p90_loss_db": round(float(np.percentile(v, 90)), 4),
                    "mean_loss_db": round(float(np.mean(v)), 4),
                })  # fmt: skip
    out_csv = out_csv or (HERE / "paper_data" / "accuracy_losses.csv")
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    with out_csv.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    return out_csv


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("cmd", choices=("profile", "pilot", "run", "merge"))
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--drops", type=int, nargs="+", default=None)
    p.add_argument("--arrays", type=int, nargs="+", default=list(ARRAYS))
    a = p.parse_args()
    if a.cmd == "profile":
        _threads1()
        for n in a.arrays:
            for tg in TARGETS:
                t0 = time.time()
                profile(n, tg)
                print(f"profiled {n}x{n} {tg} in {time.time() - t0:.1f} s", flush=True)
    elif a.cmd == "pilot":
        for n in a.arrays:
            print(run_job((n, "-6_14", EVAL_DROPS[0])), flush=True)
    elif a.cmd == "run":
        jobs = all_jobs(tuple(a.drops) if a.drops else EVAL_DROPS, tuple(a.arrays))
        jobs.sort(key=lambda j: -j[0])  # the largest arrays first
        with ProcessPoolExecutor(max_workers=a.workers) as ex:
            for msg in ex.map(run_job, jobs):
                print(msg, flush=True)
    else:
        print(merge(tuple(a.drops) if a.drops else EVAL_DROPS))


if __name__ == "__main__":
    main()
