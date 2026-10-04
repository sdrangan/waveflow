"""campaign.py — run the builds of the split and merge their measurements.

Steps 5.2, 5.3 and 5.7 of ``plans/mimo_cg/mimo_cg_paper_sims.md``.  The grid has one axis, the build
label of ``paper_data/holdout_split.csv``; one point is one :func:`~examples.mimo_cg.hw.measure.measure`
(csynth, attribution, traced RTL run, extraction), driven by the framework's
:class:`~waveflow.build.sweep.SweepRunner`, which isolates failures, saves after every point and
resumes.  The runner is serial, so a campaign is parallelized as **shards**, each its own process with
its own summary::

    python -m examples.mimo_cg.hw.campaign --dry-run                      # no toolchain
    python -m examples.mimo_cg.hw.campaign --role fit --shard 0/4         # one of four processes
    python -m examples.mimo_cg.hw.campaign --role fit --shard 0/4 --resume
    python -m examples.mimo_cg.hw.campaign --merge fit                    # tables + platform records
    python -m examples.mimo_cg.hw.campaign --reattribute fit              # re-read the reports

``--merge`` writes three tables to ``paper_data/`` from the per-build records:

* ``hw_builds.csv`` — one row per build: its knobs, the totals, the clock and the wall time;
* ``hw_modules.csv`` — one row per module of each build, one per pipelined loop inside a module
  (``subblock`` rows: a part of their module's row, not an addition to it), and one per channel of
  the top (the stream-of-blocks memories, the FIFOs and the bus adapters);
* ``hw_cycles.csv`` — the job intervals and their fit, and the block spans.

It also rebuilds the work platform's module store from the ``fit`` builds, serially.  **Held-out
builds are never filed there**, and are merged only when ``--merge fit holdout`` asks for them, which
plan step 5.7 does after the models are committed.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from examples.mimo_cg.hw import build as B
from examples.mimo_cg.hw import measure as M
from examples.mimo_cg.hw.space import HwConfig, read_split
from examples.mimo_cg.mimo_cg import provenance, write_table
from waveflow.build.build import BuildConfig, BuildDag, BuildStep
from waveflow.build.sweep import ParamGrid, Stage, SweepRunner, sweep_cli

HERE = Path(__file__).resolve().parent
EXAMPLE = HERE.parent
PAPER_DATA = EXAMPLE / "paper_data"
KNOBS = tuple(HwConfig.__dataclass_fields__)
COUNTERS = ("lut", "ff", "dsp", "bram", "uram")


def split() -> dict[str, tuple[str, str, HwConfig]]:
    """``{build: (top, role, configuration)}`` of the committed split, in file order."""
    return {b: (t, r, c) for b, t, r, c in read_split()}


def grid() -> ParamGrid:
    """One axis: the build."""
    return ParamGrid(build=tuple(split()))


def shard(builds: list[str], spec: str) -> list[str]:
    """Shard ``i/n`` of ``builds``: every n-th build from the i-th, so shards mix sizes."""
    i, n = (int(x) for x in spec.split("/"))
    if not 0 <= i < n:
        raise ValueError(f"shard {spec!r}: need 0 <= i < n")
    return builds[i::n]


# --- the steps -------------------------------------------------------------------------------


@dataclass(kw_only=True)
class HwPointStep(BuildStep):
    description = (
        "Measure one build: csynth, attribution, the traced RTL run, extraction."
    )
    consumes: ClassVar[list] = []
    produces: ClassVar[dict] = {"hw_point": Path("results/hw_points/last_point.txt")}
    params: ClassVar[dict] = {"build": ""}

    def run(self, config: BuildConfig, **kw) -> dict:
        top, role, c = split()[kw["build"]]
        rec = M.measure(kw["build"], top, c, role=role)
        if "error" in rec:
            raise RuntimeError(rec["error"])
        marker = M.POINTS_DIR / "last_point.txt"
        marker.write_text(f"{kw['build']}\n", encoding="utf-8")
        return {"hw_point": marker}


@dataclass(kw_only=True)
class DryPointStep(BuildStep):
    description = (
        "Pre-flight one build: elaborate it and budget its RTL run, with no toolchain."
    )
    consumes: ClassVar[list] = []
    produces: ClassVar[dict] = {"hw_dry": Path("results/hw_points/dry_point.txt")}
    params: ClassVar[dict] = {"build": ""}

    def run(self, config: BuildConfig, **kw) -> dict:
        from waveflow.build.elaborate import elaborate

        top, role, c = split()[kw["build"]]
        elaborate(M.comp_class(top), M.elab_params(top, c), name=M.TOP_NAME[top])
        _problems, jobs = M.workload(top, c, kw["build"])
        path = M.POINTS_DIR / "dry_point.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"{kw['build']} {role} jobs={jobs} n_cycles={M.cycles_bound(top, c, jobs)}\n",
            encoding="utf-8",
        )
        return {"hw_dry": path}


def build_campaign_dag() -> BuildDag:
    dag = BuildDag()
    dag.add(HwPointStep(name="hw_point"))
    dag.add(DryPointStep(name="hw_dry"))
    return dag


# --- the merge -------------------------------------------------------------------------------


def _records(roles: tuple[str, ...], points_dir: Path) -> list[dict]:
    """The records of every build of ``roles``, in split order; raises if one is missing."""
    recs, missing = [], []
    for build, (_top, role, _c) in split().items():
        if role not in roles:
            continue
        path = points_dir / f"{build}.json"
        if path.is_file():
            recs.append(json.loads(path.read_text(encoding="utf-8")))
        else:
            missing.append(build)
    if missing:
        raise FileNotFoundError(
            f"{len(missing)} build(s) not measured yet: {missing[:5]} ..."
        )
    return recs


def _head(rec: dict) -> dict:
    return {"build": rec["build"], "top": rec["top"], "role": rec["role"]}


def build_rows(recs: list[dict]) -> list[dict]:
    rows = []
    for rec in recs:
        res = rec.get("resources", {})
        total, integ = res.get("total", {}), res.get("integration", {})
        rows.append(
            _head(rec)
            | {k: rec["config"][k] for k in KNOBS}
            | {"est_ns": res.get("est_ns", "")}
            | {k: total.get(k, "") for k in COUNTERS}
            | {f"integ_{k}": integ.get(k, "") for k in ("lut", "ff", "bram")}
            | {
                "bit_exact": int(rec.get("rtl", {}).get("bit_exact", False)),
                "csynth_s": rec.get("csynth_seconds", ""),
                "xsi_s": rec.get("xsi_seconds", ""),
                "error": rec.get("error", ""),
            }
        )
    return rows


def module_rows(recs: list[dict]) -> list[dict]:
    rows = []
    for rec in recs:
        res = rec.get("resources", {})
        for m in res.get("modules", []):
            rows.append(
                _head(rec)
                | {"kind": "module", "name": m["cls"], "rtl_module": m["rtl_module"]}
                | {k: m[k] for k in COUNTERS}
                | {"words": "", "bits": "", "banks": ""}
            )
            for loop, sub in m.get("subblocks", {}).items():
                rows.append(
                    _head(rec)
                    | {
                        "kind": "subblock",
                        "name": f"{m['cls']}.{loop}",
                        "rtl_module": "",
                    }
                    | {k: sub.get(k, 0) for k in COUNTERS}
                    | {"words": "", "bits": "", "banks": ""}
                )
        for ch in res.get("channels", []):
            rows.append(
                _head(rec)
                | {"kind": ch["kind"], "name": ch["name"], "rtl_module": ""}
                | {k: ch.get(k, 0) for k in COUNTERS}
                | {k: ch[k] for k in ("words", "bits", "banks")}
            )
    return rows


def cycle_rows(recs: list[dict]) -> list[dict]:
    """Long format: ``quantity`` is ``job_interval`` (one row per job, with its ``nit``), a fit term
    (``first_done``, ``t0``, ``t_iter``, ``max_resid``) or a span kind (``mm.iter`` …).
    """
    blank = dict.fromkeys(("nit", "n", "stalled", "wait_n", "wait_min", "wait_max"), "")
    blank |= dict.fromkeys(("b2b_n", "b2b_min", "b2b_max"), "")
    rows = []
    for rec in recs:
        fit = rec.get("intervals")
        if fit:
            for nit, cyc in zip(fit["nit"], fit["interval"], strict=True):
                rows.append(
                    _head(rec)
                    | {"quantity": "job_interval", "cycles": cyc}
                    | blank
                    | {"nit": nit}
                )
            for q in ("first_done", "t0", "t_iter", "max_resid"):
                rows.append(
                    _head(rec)
                    | {"quantity": q, "cycles": round(float(fit[q]), 6)}
                    | blank
                )
        for kind, s in rec.get("spans", {}).items():
            rows.append(
                _head(rec)
                | {"quantity": kind, "cycles": "" if s["span"] is None else s["span"]}
                | blank
                | {"n": s["n"], "stalled": s["stalled"]}
                | {
                    f"{r}_{k}": "" if s[r][k] is None else s[r][k]
                    for r in ("wait", "b2b")
                    for k in ("n", "min", "max")
                }
            )
    return rows


def merge(
    roles: tuple[str, ...],
    *,
    points_dir: Path = M.POINTS_DIR,
    out_dir: Path = PAPER_DATA,
) -> dict:
    """Write the three tables for the builds of ``roles``; returns ``{table: path}``."""
    recs = _records(roles, points_dir)
    tools = sorted({r["tool"] for r in recs})
    note = {
        "tool": "+".join(tools),
        "part": B.PART,
        "period_ns": B.PERIOD_NS,
        "roles": "+".join(roles),
    }
    out = {}
    for name, rows in (
        ("hw_builds", build_rows(recs)),
        ("hw_modules", module_rows(recs)),
        ("hw_cycles", cycle_rows(recs)),
    ):
        out[name] = Path(out_dir) / f"{name}.csv"
        write_table(out[name], rows, provenance(name, **note))
    return out


def reattribute(roles: tuple[str, ...], points_dir: Path = M.POINTS_DIR) -> int:
    """Re-read the csynth reports of measured builds into their records (no tool runs).

    For when the attribution gains detail: the reports are still on disk, so the ``resources`` of
    each record are refreshed in place.  Returns how many records were refreshed.
    """
    n = 0
    for rec in _records(roles, points_dir):
        if "resources" not in rec:
            continue
        c = HwConfig(**rec["config"])
        rec["resources"] = M.attribute(rec["top"], c, B.BUILD_ROOT / rec["build"])
        path = points_dir / f"{rec['build']}.json"
        path.write_text(json.dumps(rec, indent=1) + "\n", encoding="utf-8")
        n += 1
    return n


def file_fit_records(points_dir: Path = M.POINTS_DIR) -> int:
    """Rebuild the work platform's module store from the ``fit`` builds (never a held-out one)."""
    store = M.WORK_ROOT / M.PLATFORM / "modules"
    if store.is_dir():
        shutil.rmtree(
            store
        )  # derived data: rebuilt whole, so a re-merge cannot double-file
    n = 0
    for rec in _records(("fit",), points_dir):
        if "error" in rec:
            continue
        c = HwConfig(**rec["config"])
        n += M.file_records(
            rec["top"],
            c,
            B.BUILD_ROOT / rec["build"],
            tool=rec["tool"],
            cost_seconds=float(rec.get("csynth_seconds", 0.0)),
        )
    return n


# --- the command line ------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--merge", nargs="+", choices=("fit", "holdout"), default=None)
    pre.add_argument(
        "--reattribute", nargs="+", choices=("fit", "holdout"), default=None
    )
    pre.add_argument("--role", choices=("fit", "holdout"), default=None)
    pre.add_argument("--shard", default=None)
    known, _ = pre.parse_known_args(argv)
    rest = [a for a in (argv if argv is not None else sys.argv[1:])]
    if known.reattribute:
        print(
            f"re-attributed {reattribute(tuple(dict.fromkeys(known.reattribute)))} record(s)"
        )
        return 0
    if known.merge:
        roles = tuple(dict.fromkeys(known.merge))
        for name, path in merge(roles).items():
            print(f"{name} -> {path}")
        print(f"filed {file_fit_records()} record(s) into {M.WORK_ROOT / M.PLATFORM}")
        return 0

    labels = list(split())

    def narrow(g: ParamGrid, args) -> ParamGrid:
        chosen = [
            b
            for b in g.axes["build"]
            if args.role is None or split()[b][1] == args.role
        ]
        if args.shard:
            chosen = shard(chosen, args.shard)
        return g.subset(build=tuple(chosen))

    # One summary per (role, shard), so parallel shards do not share a file and --resume finds it.
    tag = f"{known.role or 'all'}_{(known.shard or '0/1').replace('/', 'of')}"
    runner = SweepRunner(
        dag_factory=build_campaign_dag,
        root_dir=EXAMPLE,
        summary=EXAMPLE / "results" / f"hw_campaign_{tag}.json",
    )
    print(f"hw campaign: {len(labels)} builds in the split")
    return sweep_cli(
        runner,
        grid(),
        description="mimo_cg hardware campaign (Phase 5)",
        stages=[Stage(through="hw_point", use_platform=False)],
        dry_run_stages=[Stage(through="hw_dry", use_platform=False)],
        extra_args=[
            (
                ("--role",),
                {
                    "choices": ("fit", "holdout"),
                    "default": None,
                    "help": "builds of one role",
                },
            ),
            (
                ("--shard",),
                {"default": None, "help": "i/n: every n-th build from the i-th"},
            ),
        ],
        grid_from_args=narrow,
        argv=rest,
    )


if __name__ == "__main__":
    raise SystemExit(main())
