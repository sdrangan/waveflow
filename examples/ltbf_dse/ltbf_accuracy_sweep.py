"""ltbf_accuracy_sweep.py -- the accuracy DSE, no Vitis in the loop (plan step 3.2, AC7).

Per (drop, array, target) **job**: render and estimate once (``ltbf_chain.prepare``), then the exact
float LTBF (the reference), float CG and the bit-exact hardware chain (``ltbf_fixed_chain``) for every
fixed-point configuration, every ``t`` in :data:`T_EVAL` and ``r`` 1 and 2.  A job writes its per-UE
measured SINRs to ``paper_data/acc/jobs/<array>_<target>_<drop>.json`` (local) and is skipped when that
file exists (resumable).  ``merge`` turns the jobs into ``paper_data/accuracy_losses.csv``: per
(array, target, config, t, r) the median, 10th and 90th percentile and mean of the loss (exact minus
config, dB) over the served UEs of the evaluation drops.

Formats come from ``profile`` on the **profile drops** (1-10) per (array, target); the evaluation drops
(11-100) never set a format.  Every draw is seeded and every command computes in **spawned** workers
with single-threaded BLAS (a forked worker would keep the parent's already-started BLAS threads), so a
job's file is byte for byte the same on a re-run.

    python -m examples.ltbf_dse.ltbf_accuracy_sweep profile
    python -m examples.ltbf_dse.ltbf_accuracy_sweep pilot
    python -m examples.ltbf_dse.ltbf_accuracy_sweep run --workers 4
    python -m examples.ltbf_dse.ltbf_accuracy_sweep merge
"""

from __future__ import annotations

import argparse
import csv
import json
import multiprocessing as mp
import os
import sys
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
#: The evaluation drops of each array: 32x32 trimmed to 30 (11-40) so the sweep fits its 8 h budget
#: (plan §14, 2026-10-29: four 32x32 jobs side by side ran 980 s each, three times the pilot's).
EVAL_BY_ARRAY = {8: EVAL_DROPS, 16: EVAL_DROPS, 32: tuple(range(11, 41))}
TARGETS = ("-6_3", "-6_14")
#: Operand widths (16-bit lanes up to 16, 32-bit lanes above), guard bits, forms.
WIDTHS = (10, 12, 14, 16, 20)
GUARDS = (1,)
FORMS = ("factored", "explicit")
T_EVAL = (1, 2, 3, 5, 7, 10)
RANKS = (1, 2)


_SINGLE_THREAD_ENV = {
    k: "1" for k in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS")
}


def _pool(workers: int) -> ProcessPoolExecutor:
    """Spawned workers with single-threaded BLAS: the parent's BLAS started its threads when this
    module imported numpy, so the variables only reach a fresh interpreter."""
    os.environ.update(_SINGLE_THREAD_ENV)
    return ProcessPoolExecutor(max_workers=workers, mp_context=mp.get_context("spawn"))


def profile_job(args) -> str:
    n, target = args
    t0 = time.time()
    profile(n, target)
    return f"profiled {n}x{n} {target} in {time.time() - t0:.1f} s"


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


#: AC7's pre-chosen subset (two drops over the whole grid) and where its re-run goes.
SUBSET_DROPS = (11, 12)
RERUN = OUT / "rerun"


def job_path(n: int, target: str, drop: int, jobs_dir: Path | None = None) -> Path:
    return (jobs_dir or JOBS) / f"{n}x{n}_{target}_{drop:03d}.json"


def run_job(args) -> str:
    """One job, ``(n, target, drop)`` or ``(n, target, drop, jobs_dir)``: skipped when its file exists
    (a spawned worker does not see the parent's globals, so the directory travels with the job).
    """
    n, target, drop, *rest = args
    path = job_path(n, target, drop, rest[0] if rest else None)
    if path.exists():
        return f"skip {path.name}"
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


def all_jobs(drops=None, arrays=ARRAYS, targets=TARGETS) -> list:
    """Every (array, target, drop) job: *drops*, or each array's :data:`EVAL_BY_ARRAY`."""
    return [
        (n, tg, d)
        for n in arrays
        for tg in targets
        for d in (drops or EVAL_BY_ARRAY[n])
    ]


def merge(
    drops=None, out_csv: Path | None = None, jobs_dir: Path | None = None
) -> Path:
    """The jobs of *drops* (or each array's :data:`EVAL_BY_ARRAY`) in *jobs_dir* into the losses CSV;
    a job missing from the set is an error, not a smaller sample."""
    rows = []
    for n in ARRAYS:
        for tg in TARGETS:
            ds = drops or EVAL_BY_ARRAY[n]
            files = [job_path(n, tg, d, jobs_dir) for d in ds]
            missing = [f.name for f in files if not f.exists()]
            if missing:
                raise FileNotFoundError(
                    f"{len(missing)} jobs missing, e.g. {missing[:3]}"
                )
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
    p.add_argument("cmd", choices=("profile", "pilot", "run", "merge", "subset"))
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--drops", type=int, nargs="+", default=None)
    p.add_argument("--arrays", type=int, nargs="+", default=list(ARRAYS))
    a = p.parse_args()
    if a.cmd == "profile":
        with _pool(1) as ex:
            for msg in ex.map(
                profile_job, [(n, tg) for n in a.arrays for tg in TARGETS]
            ):
                print(msg, flush=True)
    elif a.cmd == "pilot":
        # One job an array, side by side as in the run (the timing includes the contention).
        with _pool(len(a.arrays)) as ex:
            for msg in ex.map(run_job, [(n, "-6_14", EVAL_DROPS[0]) for n in a.arrays]):
                print(msg, flush=True)
    elif a.cmd == "run":
        jobs = all_jobs(tuple(a.drops) if a.drops else None, tuple(a.arrays))
        jobs.sort(key=lambda j: -j[0])  # the largest arrays first
        with _pool(a.workers) as ex:
            for msg in ex.map(run_job, jobs):
                print(msg, flush=True)
    elif a.cmd == "subset":
        sys.exit(subset(a.workers))
    else:
        print(merge(tuple(a.drops) if a.drops else None))


def subset(workers: int = 4) -> int:
    """AC7: re-run :data:`SUBSET_DROPS` over the whole grid into :data:`RERUN` (fresh, nothing
    skipped), then compare every job file and the subset's merged rows with the main run's, byte
    for byte.  Returns 0 when all are identical."""
    import filecmp
    import shutil

    shutil.rmtree(RERUN, ignore_errors=True)
    jobs_dir = RERUN / "jobs"
    jobs_dir.mkdir(parents=True)
    jobs = [(*j, jobs_dir) for j in all_jobs(SUBSET_DROPS)]
    jobs.sort(key=lambda j: -j[0])
    with _pool(workers) as ex:
        for msg in ex.map(run_job, jobs):
            print(msg, flush=True)
    bad = [j[:3] for j in jobs
           if not filecmp.cmp(job_path(*j[:3]), job_path(*j[:3], jobs_dir), shallow=False)]  # fmt: skip
    a = merge(SUBSET_DROPS, RERUN / "subset_main.csv")
    b = merge(SUBSET_DROPS, RERUN / "subset_rerun.csv", jobs_dir)
    same_rows = filecmp.cmp(a, b, shallow=False)
    print(
        f"{len(jobs) - len(bad)}/{len(jobs)} job files identical; merged rows identical: {same_rows}"
    )
    return 0 if not bad and same_rows else 1


if __name__ == "__main__":
    main()
