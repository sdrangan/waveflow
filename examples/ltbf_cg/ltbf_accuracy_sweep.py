"""ltbf_accuracy_sweep.py -- the accuracy DSE of the LTBF CG (no Vitis in the loop).

``plans/mimo_cg/ltbf_cg_dse.md`` steps 3.1-3.2 (§9 b, AC4).  Every number comes from the float
pipeline (:mod:`examples.ltbf_cg.ltbf`) and the bit-exact fixed-point model
(:mod:`examples.ltbf_cg.ltbf_fixed`, equal to Vitis ``ap_fixed`` by step 2.3).

Jobs
----
A job is one drop (or, for the N_UE sweep, one group of pooled drops) in one scenario: band,
SINR target, SRS noise, array and UEs per sector.  It draws the SRS once and scores, **paired on
that draw**, the exact LTBF, instantaneous MMSE, the float CG (regimes 1 and 2) and every
fixed-point configuration (format x ``Q`` form x regime) for every ``t <= 10`` and ``r`` in
{1, 2}.  Its per-UE SINRs go to one ``.npz`` under ``results/sweep/``; the merge turns them into
per-UE losses ``exact - approx`` (dB, signed) and their median and percentiles per configuration
in ``paper_data/accuracy_losses.csv``.

The formats are static per (band, target, SRS, array, UEs per sector, regime):
:func:`~examples.ltbf_cg.ltbf_fixed.formats_for` over a range profile of the profile drops
(1-10; pooled groups 1-5 for N_UE > 10).  The merge scores the other drops only, which are held
out from the profile and may saturate, as hardware built for a profile would.

Determinism
-----------
Every random draw is keyed by the job; the fixed-point products are exact integers (summation
order cannot change them); BLAS runs single-threaded in every worker.  So a job's output is the
same for any worker count, and the merge is a pure function of the job files.

The runner is a process pool over jobs with :class:`~waveflow.build.sweep.SweepRunner`'s concerns
(resume by output file, per-job failure isolation, a summary log, dry run); ``SweepRunner`` itself
is serial and drives build DAGs.

Run from the repo root::

    python -m examples.ltbf_cg.ltbf_accuracy_sweep --dry-run
    python -m examples.ltbf_cg.ltbf_accuracy_sweep run --workers 4
    python -m examples.ltbf_cg.ltbf_accuracy_sweep merge
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
import traceback
from dataclasses import asdict, dataclass
from functools import cache
from pathlib import Path

import numpy as np

from examples.ltbf_cg import ltbf as lt
from examples.ltbf_cg import ltbf_fixed as lf
from examples.ltbf_cg import ltbf_link as lk
from examples.ltbf_cg import scenario as sc

HERE = Path(__file__).resolve().parent
RESULTS = HERE / "results" / "sweep"
#: The static formats (an input of every job, so a re-run in another results tree reuses them).
FORMATS_DIR = HERE / "results" / "sweep_formats"
PAPER_DATA = HERE / "paper_data"

#: Seed namespace of the sweep's SRS draws (never shared with the tests or the paper check).
SEED_NS = 31
T_MAX = 10
RANKS = (1, 2)
BANDS = ("100MHz", "1.44MHz")
TARGET_NAMES = ("-6_3", "-6_14")
SRS = (None, lk.G_SRS_DB)  # noiseless, nominal
ARRAYS = (8, 16, 32)
N_DROPS = 100
#: The drops the formats are profiled on.
PROFILE_DROPS = tuple(range(1, 11))
#: Fixed-point formats: (W, guard bits).  Width at guard 1, then the guard axis at 12 and 16 bits.
FORMATS = tuple((w, 1) for w in (10, 12, 14, 16, 18, 20, 24)) + (
    (12, 0),
    (12, 2),
    (16, 0),
    (16, 2),
)
FORMS = ("factored", "explicit")
REGIMES = (1, 2)
#: Regime 2 at 32x32 (B = I has 1024 columns, ~20x regime 1's cost) runs on these drops and formats.
#: (drops 11-20, held out from the formats' profile: M3-M4 review)
R2_N32_DROPS = tuple(range(11, 21))
R2_N32_FORMATS = ((12, 1), (16, 1), (24, 1))
#: The N_UE sweep (16x16, 100 MHz, [-6, 3] dB, nominal SRS): UEs per sector, groups of pooled drops.
NUE_VALUES = (4, 10, 20, 40)
NUE_GROUPS = 25
#: BLAS threads per worker (the integer products are exact for any count; the float parts are
#: reproduced by re-running with the same count).
THREADS = 2


def fmt_name(w: int, g: int) -> str:
    return f"W{w}g{g}"


@dataclass(frozen=True)
class Job:
    band: str
    target: str
    srs: str  # "none" | "10dB"
    n: int
    nue: int  # UEs per sector
    group: int  # the drop (nue <= 10) or the pooled group

    @property
    def g_srs_db(self) -> float | None:
        return None if self.srs == "none" else float(self.srs.removesuffix("dB"))

    @property
    def path(self) -> Path:
        return (
            RESULTS / self.band / self.target / self.srs / f"n{self.n}" / f"nue{self.nue}"
            / f"g{self.group:03d}.npz"
        )  # fmt: skip

    @property
    def drops(self) -> tuple[int, ...]:
        """The traced drops this job pools: one for up to 10 UEs per sector."""
        k = max(1, -(-self.nue // sc.N_UE))
        return tuple(range((self.group - 1) * k + 1, self.group * k + 1))

    def seed(self) -> list[int]:
        return [
            SEED_NS, BANDS.index(self.band), TARGET_NAMES.index(self.target),
            0 if self.srs == "none" else 1, self.n, self.nue, self.group,
        ]  # fmt: skip


def _srs_name(g: float | None) -> str:
    return "none" if g is None else f"{g:g}dB"


def jobs(main_drops: int = N_DROPS, nue_groups: int = NUE_GROUPS) -> list[Job]:
    """Every job of the grid, the main grid first, then the N_UE sweep."""
    out = [
        Job(band, target, _srs_name(g), n, sc.N_UE, d)
        for band in BANDS
        for target in TARGET_NAMES
        for g in SRS
        for n in ARRAYS
        for d in range(1, main_drops + 1)
    ]
    out += [
        Job("100MHz", "-6_3", _srs_name(lk.G_SRS_DB), 16, nue, grp)
        for nue in NUE_VALUES
        if nue != sc.N_UE
        for grp in range(1, nue_groups + 1)
    ]
    return out


def fixed_plan(job: Job) -> list[tuple[int, int, str, int]]:
    """The fixed-point configurations a job runs: (W, g, form, regime)."""
    plan = []
    for w, g in FORMATS:
        for form in FORMS:
            for regime in REGIMES:
                big = regime == 2 and job.n >= 32
                if big and (
                    job.group not in R2_N32_DROPS or (w, g) not in R2_N32_FORMATS
                ):
                    continue
                plan.append((w, g, form, regime))
    return plan


# --- channels ---------------------------------------------------------------------------------


@cache
def _drop(d: int) -> dict:
    return sc.load_drop(d)


def channels(job: Job, snapshot: int) -> np.ndarray:
    """``S x N x U x F`` for the job: one drop, or ``nue`` UEs per sector pooled from its drops
    (the BS and its sectors are the same in every drop, so pooled UEs form one larger drop).
    """
    hs = [lk.channel(_drop(d), snapshot, job.n, job.band) for d in job.drops]
    if len(hs) == 1 and job.nue == sc.N_UE:
        return hs[0]
    cols = []
    for k, d in enumerate(job.drops):
        sect = np.asarray(_drop(d)["ue_sector"])
        for s in range(sc.N_SECT):
            take = min(sc.N_UE, job.nue - k * sc.N_UE)
            cols.append((k, np.flatnonzero(sect == s)[:take]))
    return np.concatenate([hs[k][:, :, idx, :] for k, idx in cols], axis=2)


# --- formats ----------------------------------------------------------------------------------


#: A formats key: (band, target, srs, n, nue, regime).  A regime is its own hardware, and a
#: design is built for its operating scenario, so each gets its own profile.
FormatsKey = tuple[str, str, str, int, int, int]
#: Bump when the profile's definition changes: a cached profile of another version is redone.
PROFILE_VERSION = 2


def _profile_groups(nue: int) -> tuple[int, ...]:
    return (
        PROFILE_DROPS
        if nue <= sc.N_UE
        else tuple(range(1, 1 + len(PROFILE_DROPS) // 2))
    )


def _profile_def(key: FormatsKey) -> dict:
    """Everything a profile depends on; a cached profile is used only if this matches."""
    return {
        "version": PROFILE_VERSION, "key": list(key), "groups": list(_profile_groups(key[4])),
        "t_max": T_MAX, "seed_ns": SEED_NS,
    }  # fmt: skip


def _formats_path(key: FormatsKey) -> Path:
    band, target, srs, n, nue, regime = key
    return FORMATS_DIR / f"{band}_{target}_{srs}_n{n}_nue{nue}_r{regime}.json"


def profile(key: FormatsKey) -> dict[str, float]:
    """The register ranges of one regime over the profile drops of one scenario."""
    band, target, srs, n, nue, regime = key
    ranges: dict[str, float] = {}
    for grp in _profile_groups(nue):
        job = Job(band, target, srs, n, nue, grp)
        h0 = channels(job, 0)
        pc0 = lk.power_control(h0, lk.TARGETS[target])
        ltu = lk.long_term(
            lk.scaled(h0, pc0), pc0, np.random.default_rng(job.seed()), job.g_srs_db
        )
        ranges = lf.merge_ranges(ranges, lf.profile_link(ltu, T_MAX, regimes=(regime,)))
    return ranges


def formats(key: FormatsKey) -> dict[str, lf.LtbfFormats]:
    """The static formats of one (scenario, array, UEs per sector, regime), from its profile."""
    path = _formats_path(key)
    ranges = None
    if path.is_file():
        saved = json.loads(path.read_text())
        if saved.get("definition") == _profile_def(key):
            ranges = saved["ranges"]
    if ranges is None:
        ranges = profile(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        body = {"definition": _profile_def(key), "ranges": ranges}
        tmp.write_text(json.dumps(body, indent=1, sort_keys=True))
        os.replace(tmp, path)
    return {fmt_name(w, g): lf.formats_for(w, ranges, g) for w, g in FORMATS}


def _formats_seconds(key: FormatsKey) -> tuple[FormatsKey, float]:
    t0 = time.perf_counter()
    formats(key)
    return key, time.perf_counter() - t0


def job_ranks(job: Job) -> tuple[int, ...]:
    """r = 2 only with SRS noise: with a noiseless estimate M_i is rank one in this scenario (at
    most 3 paths per link), so its second eigenvector is a round-off direction."""
    return RANKS if job.srs != "none" else RANKS[:1]


# --- one job ----------------------------------------------------------------------------------


def run_job(job: Job) -> dict:
    """Evaluate one job and write its ``.npz``.  Never raises: a failure is returned."""
    t0 = time.perf_counter()
    try:
        plan = fixed_plan(job)
        ranks = job_ranks(job)
        fmts = {
            reg: formats((job.band, job.target, job.srs, job.n, job.nue, reg))
            for reg in sorted({p[3] for p in plan})
        }
        by_fmt: dict[str, tuple[lf.LtbfFormats, list[lt.Solve]]] = {}
        for w, g, form, regime in plan:
            name = f"{fmt_name(w, g)}/r{regime}"
            by_fmt.setdefault(name, (fmts[regime][fmt_name(w, g)], []))[1].append(
                lt.Solve(form, regime)
            )
        ev = lt.evaluate_channels(
            channels(job, 0), channels(job, 1), lk.TARGETS[job.target], job.g_srs_db,
            np.random.default_rng(job.seed()), T_MAX, ranks,
            solves=tuple(lt.Solve("factored", reg) for reg in REGIMES),
            fixed={k: (f, tuple(v)) for k, (f, v) in by_fmt.items()},
        )  # fmt: skip
        ues = np.array(sorted(ev.exact[RANKS[0]]), dtype=np.int64)

        def vec(d: dict[int, float] | None) -> np.ndarray:
            if (
                d is None
            ):  # not evaluated: r = 2 without SRS noise, regime 2 at 32x32 ...
                return np.full(ues.size, np.nan)
            return np.array([d.get(int(u), np.nan) for u in ues])

        exact = np.stack([vec(ev.exact.get(r)) for r in RANKS])
        flt = np.full((len(REGIMES), T_MAX, len(RANKS), ues.size), np.nan)
        fixed = np.full(
            (len(FORMATS), len(FORMS), len(REGIMES), T_MAX, len(RANKS), ues.size),
            np.nan,
        )
        for k, reg in enumerate(REGIMES):
            for t in range(1, T_MAX + 1):
                for q, r in enumerate(RANKS):
                    flt[k, t - 1, q] = vec(ev.approx.get(("factored", reg, t, r)))
                    for i, (w, g) in enumerate(FORMATS):
                        for j, form in enumerate(FORMS):
                            key = (f"{fmt_name(w, g)}/r{reg}", form, reg, t, r)
                            fixed[i, j, k, t - 1, q] = vec(ev.fixed.get(key))
        job.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = job.path.with_name(job.path.stem + ".tmp.npz")
        np.savez(tmp, ues=ues, exact=exact, ins=vec(ev.ins), float=flt, fixed=fixed)
        os.replace(tmp, job.path)
        return {"job": asdict(job), "ok": True, "seconds": round(time.perf_counter() - t0, 2),
                "ues": int(ues.size), "configs": len(plan)}  # fmt: skip
    except Exception as exc:  # noqa: BLE001
        # a failed job is a datapoint, the sweep goes on
        return {"job": asdict(job), "ok": False, "error": f"{type(exc).__name__}: {exc}",
                "trace": traceback.format_exc(limit=5),
                "seconds": round(time.perf_counter() - t0, 2)}  # fmt: skip


def _init_worker() -> None:
    from threadpoolctl import threadpool_limits

    threadpool_limits(THREADS)


def run(todo: list[Job], workers: int, log: Path) -> int:
    """Run the jobs not yet done in a pool; append each result to ``log``; return the failures."""
    from multiprocessing import get_context

    todo = [j for j in todo if not j.path.is_file()]
    # the costliest first (large arrays, regime 2 at 32x32), so the pool's tail is short
    todo.sort(
        key=lambda j: (
            -j.n * (2 if j.n >= 32 and j.group in R2_N32_DROPS else 1),
            -j.nue,
        )
    )
    keys = sorted(
        {
            (j.band, j.target, j.srs, j.n, j.nue, p[3])
            for j in todo
            for p in fixed_plan(j)
        },
        key=lambda k: (-k[3] * k[5], k),
    )
    log.parent.mkdir(parents=True, exist_ok=True)
    failures = 0
    ctx = get_context("fork")
    with ctx.Pool(workers, initializer=_init_worker) as pool, log.open("a") as fh:
        # the formats first: each key profiled once, before any job needs it
        t0 = time.perf_counter()
        for key, sec in pool.imap_unordered(_formats_seconds, keys):
            print(f"formats {key}: {sec:.1f} s", flush=True)
        print(
            f"{len(keys)} format profiles: {(time.perf_counter() - t0) / 60:.1f} min",
            flush=True,
        )
        t0 = time.perf_counter()
        for i, rec in enumerate(pool.imap_unordered(run_job, todo, chunksize=1), 1):
            fh.write(json.dumps(rec) + "\n")
            fh.flush()
            failures += not rec["ok"]
            if i % 10 == 0 or not rec["ok"] or i == len(todo):
                el = time.perf_counter() - t0
                print(f"[{i}/{len(todo)}] {el / 60:.1f} min, eta {el / i * (len(todo) - i) / 60:.1f} min, "
                      f"{failures} failed; last {rec['job']} {rec['seconds']} s"
                      + ("" if rec["ok"] else f" ERROR {rec['error']}"), flush=True)  # fmt: skip
    return failures


# --- merge ------------------------------------------------------------------------------------

COLUMNS = (
    "band", "target", "srs", "n", "nue", "kind", "format", "form", "regime", "t", "r",
    "samples", "median_loss_db", "p90_loss_db", "p10_loss_db", "mean_loss_db", "median_sinr_db",
)  # fmt: skip


def _stats(loss: np.ndarray, sinr: np.ndarray) -> list[str]:
    ok = np.isfinite(loss)
    if not ok.any():
        return [str(0)] + ["nan"] * 5
    lo = loss[ok]
    return [
        str(int(ok.sum())),
        *(f"{v:.6f}" for v in (np.median(lo), np.percentile(lo, 90), np.percentile(lo, 10), lo.mean())),
        f"{np.median(sinr[ok]):.6f}",
    ]  # fmt: skip


def profile_group(j: Job) -> bool:
    """Is the job's drop (or pooled group) one the formats were profiled on?"""
    return j.group in _profile_groups(j.nue)


def merge(out: Path, todo: list[Job], exclude_profile: bool = True) -> Path:
    """Per scenario and configuration, the per-UE loss statistics over the jobs' UEs -- the drops
    held out from the formats' profile only, unless ``exclude_profile`` is off."""
    scen: dict[tuple, list[Job]] = {}
    for j in todo:
        if exclude_profile and profile_group(j):
            continue
        scen.setdefault((j.band, j.target, j.srs, j.n, j.nue), []).append(j)
    rows = []
    for key in sorted(scen, key=lambda k: (BANDS.index(k[0]), k[1], k[2], k[3], k[4])):
        files = [
            j.path for j in sorted(scen[key], key=lambda j: j.group) if j.path.is_file()
        ]
        if not files:
            continue
        parts = [np.load(f) for f in files]
        exact = np.concatenate([p["exact"] for p in parts], axis=-1)  # (R, U)
        ins = np.concatenate([p["ins"] for p in parts])
        flt = np.concatenate([p["float"] for p in parts], axis=-1)  # (reg, T, R, U)
        fixed = np.concatenate(
            [p["fixed"] for p in parts], axis=-1
        )  # (F, form, reg, T, R, U)
        base = [key[0], key[1], key[2], str(key[3]), str(key[4])]
        for q, r in enumerate(RANKS):
            rows.append(
                base
                + ["exact", "-", "-", "-", "-", str(r)]
                + _stats(ins - exact[q], exact[q])
            )
        for k, reg in enumerate(REGIMES):
            for t in range(1, T_MAX + 1):
                for q, r in enumerate(RANKS):
                    a = flt[k, t - 1, q]
                    rows.append(
                        base
                        + ["float", "-", "-", str(reg), str(t), str(r)]
                        + _stats(exact[q] - a, a)
                    )
        for i, (w, g) in enumerate(FORMATS):
            for jf, form in enumerate(FORMS):
                for k, reg in enumerate(REGIMES):
                    if not np.isfinite(fixed[i, jf, k]).any():
                        continue
                    for t in range(1, T_MAX + 1):
                        for q, r in enumerate(RANKS):
                            a = fixed[i, jf, k, t - 1, q]
                            rows.append(base + ["fixed", fmt_name(w, g), form, str(reg), str(t), str(r)]
                                        + _stats(exact[q] - a, a))  # fmt: skip
    # a configuration a scenario did not evaluate (r = 2 without SRS noise ...) has no row
    rows = [row for row in rows if row[COLUMNS.index("samples")] != "0"]
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(COLUMNS)
        w.writerows(rows)
    return out


# --- the command line -------------------------------------------------------------------------


def _select(args) -> list[Job]:
    sel = jobs(args.drops, args.nue_groups)
    if args.only_drops:
        keep = set(args.only_drops)
        sel = [j for j in sel if j.group in keep]
    if args.band:
        sel = [j for j in sel if j.band in args.band]
    if args.n:
        sel = [j for j in sel if j.n in args.n]
    if args.nue:
        sel = [j for j in sel if j.nue in args.nue]
    return sel


def main(argv: list[str] | None = None) -> int:
    global RESULTS, THREADS
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("cmd", nargs="?", default="run", choices=("run", "merge", "list"))
    p.add_argument(
        "--dry-run", action="store_true", help="List the jobs left and exit."
    )
    p.add_argument("--workers", type=int, default=4)
    p.add_argument(
        "--threads", type=int, default=THREADS, help="BLAS threads per worker."
    )
    p.add_argument("--drops", type=int, default=N_DROPS, help="Drops of the main grid.")
    p.add_argument("--nue-groups", type=int, default=NUE_GROUPS)
    p.add_argument(
        "--only-drops", type=int, nargs="+", help="Only these drops / groups."
    )
    p.add_argument("--band", nargs="+", choices=BANDS)
    p.add_argument("--n", type=int, nargs="+", choices=ARRAYS)
    p.add_argument("--nue", type=int, nargs="+", choices=NUE_VALUES)
    p.add_argument("--results", default=str(RESULTS), help="The job files' root.")
    p.add_argument("--out", default=str(PAPER_DATA / "accuracy_losses.csv"))
    args = p.parse_args(argv)
    RESULTS = Path(args.results)
    THREADS = args.threads
    sel = _select(args)
    left = [j for j in sel if not j.path.is_file()]
    if args.dry_run or args.cmd == "list":
        print(f"{len(sel)} jobs selected, {len(left)} left; {len(FORMATS)} formats")
        return 0
    if args.cmd == "run":
        fails = run(sel, args.workers, RESULTS / "sweep_log.jsonl")
        if fails:
            print(f"{fails} job(s) failed; see {RESULTS / 'sweep_log.jsonl'}")
            return 1
        return 0
    print(f"merged -> {merge(Path(args.out), sel)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
