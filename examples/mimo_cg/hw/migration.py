"""migration.py — the study's hardware rebuilt on Waveflow's components, and compared.

Step 9.4 of ``plans/mimo_cg/mimo_cg_paper_sims.md`` (gate 9.0 decision record, items 4–6, and its
amendment at step 9.4a, §14).  Phase 9 rebuilt the detector on ``SystolicCore`` and
``CgVectorCore`` and made the study's unit builds the components' standalone units.  Here every
build the Phase 5 models were fitted and tested on (132) and the 12 finalists are measured again,
and each is compared with its old measurement, which stays as committed:

    python -m examples.mimo_cg.hw.migration list [--check]   # the builds and the predictions
    python -m examples.mimo_cg.hw.campaign --role migration --shard i/4 [--resume]
    python -m examples.mimo_cg.hw.campaign --merge migration    # migration_{builds,modules,cycles}.csv
    python -m examples.mimo_cg.hw.migration finalists --shard i/3   # csynth, RTL, Vivado (long)
    python -m examples.mimo_cg.hw.migration finalists-table      # migration_finalists.csv
    python -m examples.mimo_cg.hw.migration compare              # migration_compare.csv, _metrics.csv

The list (``paper_data/migration_list.csv``) is committed before any of its builds runs.  A build is
labelled ``mig_<old label>`` and keeps its old build's knobs and workload seed; its records go to
``results/migration_points/`` and its tree to ``hw/build/mig_<old label>/`` (both gitignored).

The predictions
---------------
DSP and block RAM are counted quantities.  A detector's is predicted as its old total, minus its old
blocks (the measured ``CgMm`` and ``CgVec`` rows) and its old ``A`` channel, plus the two cores'
counted rules (:mod:`waveflow.linalg.cost`, :mod:`waveflow.linalg.cg_cost`) and the ``A`` channel in
its new shape (lane groups instead of K-lane rows) by the study's channel rule
(:func:`~examples.mimo_cg.hw.models.sob_memory`).  A finalist's old blocks and channel come from the
study's counted rules, which reproduce every one of the 28 detectors' rows (its module rows were not
committed).  A unit build is compared on its block's row, predicted by the core's counted rule.

The acceptance (§14 gate 9.0, item 5, as amended)
-------------------------------------------------
(a) every RTL output bit-exact; (b) csynth DSP and block RAM equal to the prediction, and a
finalist's implemented DSP and block RAM changed by no more than its csynth change; (c) cycles:
``t_iter`` within 5%, ``t0`` within 100 cycles, every job interval and finalist job time within 5%
plus 100 cycles, every block span within 5% or 20 cycles; (d) detector csynth and finalist
implemented LUT and FF within 20%, block rows compared and not bounded; (e) every finalist meets
4 ns after implementation; (f) every difference attributed to a module and a pre-registered cause
(:data:`CAUSES`).  The brute-force trigger is :func:`trigger`.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

from examples.mimo_cg.hw import build as B
from examples.mimo_cg.hw import models as MD
from examples.mimo_cg.hw.common import DEFAULT_N, hw_format
from examples.mimo_cg.hw.space import (
    HwConfig,
    read_split,
    read_supplement,
    read_v2,
)
from examples.mimo_cg.mimo_cg import provenance, read_table, write_table

HERE = Path(__file__).resolve().parent
EXAMPLE = HERE.parent
PAPER_DATA = EXAMPLE / "paper_data"
LIST = PAPER_DATA / "migration_list.csv"
POINTS = EXAMPLE / "results" / "migration_points"
ROLE = "migration"
PREFIX = "mig_"
KNOBS = tuple(HwConfig.__dataclass_fields__)
COUNTERS = ("lut", "ff", "dsp", "bram")
#: The old blocks and their cores.
OLD_BLOCK = {"mm": "CgMm", "vec": "CgVec"}
NEW_BLOCK = {"mm": "SystolicCore", "vec": "CgVectorCore"}

#: The bounds of the acceptance (gate 9.0, item 5, as amended at step 9.4a).
LUT_FF_PCT = 20.0
CYCLE_PCT = 5.0
T0_CYCLES = 100
SPAN_FLOOR = 20
CLOCK_NS = 4.0
#: The brute-force trigger: Spearman's rho below this, or a reordered finalist pair further apart.
RHO_MIN = 0.95
PAIR_PCT = 5.0

#: The causes a difference is attributed to, by the module or quantity it is in (pre-registered).
CAUSES = {
    "mm": "the systolic core: run-time dimensions, the A^H path, A's build-up moved into the core, "
    "the conjugated operand one bit wider, index products",
    "vec": "the CG core: run-time dimensions, index products",
    "CgLoad": "A in L-lane groups instead of K-lane rows; Waveflow's wf_load_matrix",
    "CgStore": "Waveflow's wf_store_matrix",
    "CgCtrl": "one command per core per job",
    "a_blk": "the A store's shape (L-lane groups instead of K-lane rows)",
    "queue": "one command per core per job (64-bit words, unframed)",
    "t0": "the per-job work of the cores: the CG core's start and the systolic core's A load",
    "t_iter": "the cores' per-iteration loops (the spans mm.iter and vec.iter)",
    "integration": "the top's channels, FIFOs and adapters: the A channel's shape and the "
    "64-bit command queues",
    "placed": "a module the migration did not change: Vivado optimizes across module "
    "boundaries, so its implemented size moves with its neighbours",
    "other": "not expected to change; investigated and logged in the plan's §15",
    "total": "the sum of this build's module and channel rows (their own causes)",
    "impl": "the sum of this finalist's implemented module rows (their own causes)",
    "csynth": "the cores, the loader's A and the A channel, as in the detectors' module rows",
    "timing": "the critical path moves with synthesis and placement; it is checked against the "
    "clock, not against the old value",
}


def new_label(old: str) -> str:
    return PREFIX + old


def old_label(build: str) -> str:
    return build.removeprefix(PREFIX)


# --- the cores' counted rules -----------------------------------------------------------------


def cores(top: str, c: HwConfig) -> dict:
    """The cores of a build, ``{"mm": SystolicCore, "vec": CgVectorCore}`` as it has them, sized
    as the detector and the unit builds size them."""
    from waveflow.linalg.cg_vector import CgVectorCore
    from waveflow.linalg.systolic import SystolicCore
    from waveflow.simulation.simulation import Simulation

    f, sim, K = hw_format(c.fmt), Simulation(), c.K
    out: dict = {}
    if top in ("det", "mm"):
        out["mm"] = SystolicCore(
            name="mm",
            sim=sim,
            Mmax=K,
            Kmax=K,
            Nmax=DEFAULT_N,
            L=c.L,
            R=c.R or K,
            C=c.C,
            form=c.cmul,
            sob_depth=c.sob_depth,
            a=f.A,
            b=f.P,
            c=f.S,
        )
    if top in ("det", "vec"):
        out["vec"] = CgVectorCore(
            name="vec",
            sim=sim,
            Kmax=K,
            Nmax=DEFAULT_N,
            nitmax=K,
            L=c.L,
            sob_depth=c.sob_depth,
            formats=f,
        )
    return out


def counted(core) -> dict:
    """The core's counted DSP and block RAM, by its component's model."""
    from waveflow.linalg import cost

    task = (
        "systolic_core_task"
        if type(core).__name__ == "SystolicCore"
        else "cg_vector_task"
    )
    pred = cost.resource_model(task, type(core)).predict(core)
    return {k: round(float(pred[k])) for k in ("dsp", "bram")}


def a_channel_bram(c: HwConfig, new: bool) -> int:
    """The detector's ``A`` channel in block RAM: K-lane rows (old) or L-lane groups (new), by
    the study's channel rule.  The new channel is written as ``B`` is, so it is dual-ported when
    ``B``'s is."""
    if not new:
        (a,) = [ch for ch in MD.channels(c, "det") if ch[0] == "a_blk"]
        return int(MD.sob_memory(*a[1:])["bram"])
    from waveflow.linalg.lanes import n_groups

    W = hw_format(c.fmt).A.W
    dual = c.L < c.mem_dw // 32
    return int(
        MD.sob_memory(n_groups(c.K * c.K, c.L), 2 * W * c.L, c.sob_depth, dual)["bram"]
    )


# --- the list ---------------------------------------------------------------------------------


def _old_rows() -> tuple[dict, dict]:
    builds = {r["build"]: r for r in read_table(PAPER_DATA / "hw_builds.csv")}
    blocks: dict = {}
    for r in read_table(PAPER_DATA / "hw_modules.csv"):
        if r["kind"] == "module" and r["name"] in OLD_BLOCK.values():
            blocks[(r["build"], r["name"])] = {k: int(r[k]) for k in COUNTERS}
    return builds, blocks


def studied() -> list[tuple[str, str, str, HwConfig]]:
    """The 132 builds of the Phase 5 calibration and held-out sets, in the campaign's order."""
    return [*read_split(), *read_supplement(), *read_v2()]


def finalists() -> list[dict]:
    rows = read_table(PAPER_DATA / "finalists.csv")
    return [
        {"name": r["name"], "build": r["build"], "nit": int(r["nit"])}
        | {"config": HwConfig(**{k: int(r[k]) for k in KNOBS})}
        for r in rows
    ]


def list_rows() -> list[dict]:
    """One row per build to measure: the 132 studied builds, then the 12 finalists."""
    old_builds, old_blocks = _old_rows()
    impl = {r["build"]: r for r in read_table(PAPER_DATA / "finalists_impl.csv")}
    items = [(b, t, r, c) for b, t, r, c in studied()]
    items += [(f["build"], "det", "finalist", f["config"]) for f in finalists()]
    rows = []
    for old, top, role, c in items:
        new = {k: 0 for k in ("dsp", "bram")}
        for core in cores(top, c).values():
            for k, v in counted(core).items():
                new[k] += v
        row = {"build": new_label(old), "old": old, "top": top, "role": role}
        row |= {k: getattr(c, k) for k in KNOBS}
        if role == "finalist":  # its old blocks by the study's counted rules
            old_tot = {k: int(impl[old][f"csynth_{k}"]) for k in ("dsp", "bram")}
            mm, vec = MD.mm_counted(c), MD.vec_counted(c)
            old_blk = {k: int(mm[k]) + int(vec[k]) for k in ("dsp", "bram")}
        else:
            old_tot = {k: int(old_builds[old][k]) for k in ("dsp", "bram")}
            names = [OLD_BLOCK[t] for t in cores(top, c)]
            old_blk = {
                k: sum(old_blocks[(old, n)][k] for n in names) for k in ("dsp", "bram")
            }
        row |= {f"old_block_{k}": old_blk[k] for k in ("dsp", "bram")}
        row |= {f"new_block_{k}": new[k] for k in ("dsp", "bram")}
        if top == "det":
            a_old, a_new = a_channel_bram(c, False), a_channel_bram(c, True)
            row |= {
                "old_dsp": old_tot["dsp"],
                "old_bram": old_tot["bram"],
                "old_a_bram": a_old,
                "new_a_bram": a_new,
                "pred_dsp": old_tot["dsp"] - old_blk["dsp"] + new["dsp"],
                "pred_bram": old_tot["bram"]
                - old_blk["bram"]
                - a_old
                + new["bram"]
                + a_new,
            }
        else:  # a unit build is compared on its block's row
            row |= dict.fromkeys(
                ("old_dsp", "old_bram", "old_a_bram", "new_a_bram"), ""
            )
            row |= {"pred_dsp": new["dsp"], "pred_bram": new["bram"]}
        rows.append(row)
    return rows


def write_list(path: Path = LIST) -> Path:
    from waveflow.linalg.cost import platform

    note = provenance(
        "migration_list",
        platform=platform().name,
        part=B.PART,
        period_ns=B.PERIOD_NS,
    )
    write_table(path, list_rows(), note)
    return path


def read_list(path: Path = LIST) -> list[dict]:
    rows = read_table(path) if Path(path).is_file() else []
    for r in rows:
        r["config"] = HwConfig(**{k: int(r[k]) for k in KNOBS})
    return rows


def campaign_builds(path: Path = LIST) -> list[tuple[str, str, str, HwConfig]]:
    """The campaign's builds of the list: ``(build, top, "migration", configuration)``; the
    finalists run apart (:func:`build_finalist`)."""
    return [
        (r["build"], r["top"], ROLE, r["config"])
        for r in read_list(path)
        if r["role"] != "finalist"
    ]


# --- the finalists ----------------------------------------------------------------------------


def build_finalist(row: dict) -> dict:
    """One finalist through csynth and the RTL run (the steady job list, no waveform, the build
    kept for Vivado), then implementation, as step 6.7 did.  Returns ``{build, ok, error}``.
    """
    from examples.mimo_cg.hw import impl_check
    from examples.mimo_cg.hw import measure as M

    name, c = row["build"], row["config"]
    path = POINTS / f"{name}.json"
    if not (path.is_file() and "error" not in json.loads(path.read_text())):
        rec = M.measure(
            name,
            "det",
            c,
            role="finalist",
            steady=True,
            trace=False,
            points_dir=POINTS,
            workload_label=row["old"],
        )
        if "error" in rec:
            return {"build": name, "ok": False, "error": rec["error"]}
    if not impl_check.implemented(name):
        impl_check.run_impl(name)
    return {"build": name, "ok": impl_check.implemented(name), "error": ""}


def finalist_rows() -> list[dict]:
    """One row per finalist: csynth and implemented resources, the job time and the clock."""
    from examples.mimo_cg.hw import impl_check

    out = []
    for f in [r for r in read_list() if r["role"] == "finalist"]:
        name = f["build"]
        rec = json.loads((POINTS / f"{name}.json").read_text(encoding="utf-8"))
        impl = impl_check.parse_report(impl_check.report_path(name).read_text())
        old = {r["build"]: r for r in read_table(PAPER_DATA / "finalists_impl.csv")}[
            f["old"]
        ]
        secs = B.BUILD_ROOT / name / "impl.seconds"
        row = {"name": old["name"], "build": name, "old": f["old"], "nit": old["nit"]}
        for k in COUNTERS:
            row |= {
                f"csynth_{k}": rec["resources"]["total"][k],
                f"impl_{k}": impl[k],
            }
        row |= {
            "rtl_job": rec["intervals"]["steady"][str(int(old["nit"]))],
            "bit_exact": int(rec["rtl"]["bit_exact"]),
            "csynth_est_ns": rec["resources"]["est_ns"],
            "cp_post_impl_ns": impl["cp_post_impl"],
            "timing_met": impl["timing_met"],
            "impl_seconds": float(secs.read_text()) if secs.is_file() else "",
            "tool": impl["tool"],
        }
        out.append(row)
    return out


def write_finalists(out_dir: Path = PAPER_DATA) -> Path:
    rows = finalist_rows()
    note = provenance(
        "migration_finalists",
        tool="+".join(sorted({r["tool"] for r in rows})),
        hls=_hls_tool(),
        part=B.PART,
        period_ns=B.PERIOD_NS,
        flow="export_design -flow impl",
    )
    path = Path(out_dir) / "migration_finalists.csv"
    write_table(path, rows, note)
    write_table(Path(out_dir) / FINALIST_MODULES, finalist_module_rows(), note)
    return path


def _tools(data_dir: Path) -> str:
    """The tools the compared measurements carry: Vitis HLS (the builds) and Vivado (the
    finalists' implementation), from the merged tables' headers."""
    import re

    tools = []
    for name in ("migration_builds.csv", "migration_finalists.csv"):
        path = Path(data_dir) / name
        if path.is_file():
            head = path.read_text(encoding="utf-8").splitlines()[0]
            if m := re.search(r"tool=([^,]+)", head):
                tools.append(m.group(1))
    return "+".join(tools)


def _hls_tool() -> str:
    """The Vitis HLS version of the migration's csynth (``migration_builds.csv``'s header)."""
    import re

    head = (PAPER_DATA / "migration_builds.csv").read_text().splitlines()[0]
    return re.search(r"tool=([^,]+)", head).group(1)


# --- the comparison ---------------------------------------------------------------------------


def spearman(a, b) -> float:
    """Spearman's rho of two sequences (average ranks for ties); 1 when both are constant."""
    import pandas as pd

    ra = pd.Series(np.asarray(a, float)).rank().to_numpy()
    rb = pd.Series(np.asarray(b, float)).rank().to_numpy()
    if np.ptp(ra) == 0 and np.ptp(rb) == 0:
        return 1.0
    if np.ptp(ra) == 0 or np.ptp(rb) == 0:
        return 0.0
    return float(np.corrcoef(ra, rb)[0, 1])


def _pct(old: float, new: float) -> float | str:
    return round(100.0 * (new - old) / old, 3) if old else ""


def _row(build, top, role, scope, quantity, old, new, *, pred="", ok="", cause=""):
    return {
        "build": build,
        "top": top,
        "role": role,
        "scope": scope,
        "quantity": quantity,
        "old": old,
        "new": new,
        "delta": "" if "" in (old, new) else round(float(new) - float(old), 6),
        "pct": "" if "" in (old, new) else _pct(float(old), float(new)),
        "pred": pred,
        "pass": "" if ok == "" else int(bool(ok)),
        "cause": cause,
    }


def _timing(old: str, new: str) -> str:
    """The cause of a clock row: none when the path did not move."""
    return CAUSES["timing"] if old != new else ""


def _scope_of(name: str, kind: str) -> str:
    """The cause key of a module or channel row of a detector."""
    if name in ("mm", "CgMm", "SystolicCore"):
        return "mm"
    if name in ("vec", "CgVec", "CgVectorCore"):
        return "vec"
    if name in ("CgLoad", "CgStore", "CgCtrl"):
        return name
    if name.startswith("a_blk"):
        return "a_blk"
    if name.startswith(("vec_q", "mm_q")):
        return "queue"
    if name == "integration":
        return "integration"
    return "other"


def _placed(name: str) -> str:
    """The cause key of an implemented module row: as :func:`_scope_of`, and a module the
    migration did not change is placed with the rest (:data:`CAUSES` ``placed``)."""
    key = _scope_of(name, "module")
    return "placed" if key == "other" else key


def _modules(path: Path) -> dict:
    """``{build: {(scope name, kind): {counter: value}}}`` with the old and new blocks under one
    name each (``mm``, ``vec``)."""
    out: dict = {}
    for r in read_table(path):
        if r["kind"] == "subblock":
            continue
        name = r["name"]
        name = {"CgMm": "mm", "SystolicCore": "mm", "CgVec": "vec"}.get(name, name)
        name = "vec" if name == "CgVectorCore" else name
        row = out.setdefault(old_label(r["build"]), {}).setdefault(
            (name, r["kind"]), dict.fromkeys(COUNTERS, 0)
        )
        for k in COUNTERS:
            row[k] += int(r[k])
    return out


def _cycles(path: Path) -> dict:
    out: dict = {}
    for r in read_table(path):
        d = out.setdefault(old_label(r["build"]), {"job_interval": [], "spans": {}})
        if r["quantity"] == "job_interval":
            d["job_interval"].append((int(r["nit"]), float(r["cycles"])))
        elif r["quantity"] in ("t0", "t_iter", "first_done", "max_resid"):
            d[r["quantity"]] = float(r["cycles"])
        elif r["quantity"] != "job_time" and r["cycles"] != "":
            d["spans"][r["quantity"]] = float(r["cycles"])
    return out


def compare(data_dir: Path = PAPER_DATA) -> tuple[list[dict], dict]:
    """The comparison rows and the per-criterion summary (the acceptance and the trigger)."""
    data_dir = Path(data_dir)
    plan = {r["old"]: r for r in read_list(data_dir / LIST.name)}
    old_b = {r["build"]: r for r in read_table(data_dir / "hw_builds.csv")}
    new_b = {
        old_label(r["build"]): r for r in read_table(data_dir / "migration_builds.csv")
    }
    old_m, new_m = (
        _modules(data_dir / f"{s}_modules.csv") for s in ("hw", "migration")
    )
    old_c, new_c = (_cycles(data_dir / f"{s}_cycles.csv") for s in ("hw", "migration"))
    rows: list[dict] = []
    for old, p in plan.items():
        if p["role"] == "finalist":
            continue
        top, role = p["top"], p["role"]
        ob, nb = old_b[old], new_b[old]
        rows.append(
            _row(
                old,
                top,
                role,
                "rtl",
                "bit_exact",
                1,
                int(nb["bit_exact"]),
                ok=nb["bit_exact"] == "1",
            )
        )
        rows.append(
            _row(
                old,
                top,
                role,
                "total",
                "est_ns",
                float(ob["est_ns"]),
                float(nb["est_ns"]),
                ok=float(nb["est_ns"]) <= CLOCK_NS,
                cause=_timing(ob["est_ns"], nb["est_ns"]),
            )
        )
        if top == "det":
            for k in COUNTERS:
                o, n = int(ob[k]), int(nb[k])
                cause = CAUSES["total"] if o != n else ""
                if k in ("dsp", "bram"):
                    pred = int(p[f"pred_{k}"])
                    rows.append(
                        _row(
                            old,
                            top,
                            role,
                            "total",
                            k,
                            o,
                            n,
                            pred=pred,
                            ok=n == pred,
                            cause=cause,
                        )
                    )
                else:
                    ok = abs(n - o) <= LUT_FF_PCT / 100 * o
                    rows.append(
                        _row(old, top, role, "total", k, o, n, ok=ok, cause=cause)
                    )
            # the attribution: every module and channel, old against new
            om, nm = old_m[old], new_m[old]
            for k in ("dsp", "bram"):  # the cores' own rows against their counted rules
                o = om[("mm", "module")][k] + om[("vec", "module")][k]
                n = nm[("mm", "module")][k] + nm[("vec", "module")][k]
                pred = int(p[f"new_block_{k}"])
                rows.append(
                    _row(
                        old,
                        top,
                        role,
                        "cores",
                        k,
                        o,
                        n,
                        pred=pred,
                        ok=n == pred,
                        cause=CAUSES["mm"] + "; " + CAUSES["vec"],
                    )
                )
            for key in sorted(set(om) | set(nm)):
                name, kind = key
                for k in COUNTERS:
                    o, n = om.get(key, {}).get(k, 0), nm.get(key, {}).get(k, 0)
                    if o != n:
                        rows.append(
                            _row(
                                old,
                                top,
                                role,
                                f"{kind}:{name}",
                                k,
                                o,
                                n,
                                cause=CAUSES[_scope_of(name, kind)],
                            )
                        )
        else:  # a unit build: its block's row
            nblk = "mm" if top == "mm" else "vec"
            o_row, n_row = old_m[old][(nblk, "module")], new_m[old][(nblk, "module")]
            for k in COUNTERS:
                o, n = o_row[k], n_row[k]
                ok = n == int(p[f"pred_{k}"]) if k in ("dsp", "bram") else ""
                pred = int(p[f"pred_{k}"]) if k in ("dsp", "bram") else ""
                rows.append(
                    _row(
                        old,
                        top,
                        role,
                        f"block:{NEW_BLOCK[top]}",
                        k,
                        o,
                        n,
                        pred=pred,
                        ok=ok,
                        cause=CAUSES[nblk],
                    )
                )
        oc, nc = old_c[old], new_c.get(old, {"job_interval": [], "spans": {}})
        if top == "det":
            for q, bound in (("t_iter", "pct"), ("t0", "abs")):
                o, n = oc[q], nc[q]
                ok = abs(n - o) <= (
                    CYCLE_PCT / 100 * o if bound == "pct" else T0_CYCLES
                )
                rows.append(
                    _row(old, top, role, "cycles", q, o, n, ok=ok, cause=CAUSES[q])
                )
            for i, ((nit, o), (nit2, n)) in enumerate(
                zip(oc["job_interval"], nc["job_interval"], strict=True)
            ):
                assert nit == nit2, (old, i, nit, nit2)
                ok = abs(n - o) <= CYCLE_PCT / 100 * o + T0_CYCLES
                rows.append(
                    _row(
                        old,
                        top,
                        role,
                        f"cycles:job{i + 1}",
                        f"interval_nit{nit}",
                        o,
                        n,
                        ok=ok,
                        cause=CAUSES["t0"] + "; " + CAUSES["t_iter"],
                    )
                )
        for kind, o in oc["spans"].items():
            n = nc["spans"].get(kind)
            if n is None:
                rows.append(_row(old, top, role, "span", kind, o, "", ok=False))
                continue
            ok = abs(n - o) <= max(CYCLE_PCT / 100 * o, SPAN_FLOOR)
            rows.append(
                _row(
                    old,
                    top,
                    role,
                    "span",
                    kind,
                    o,
                    n,
                    ok=ok,
                    cause=CAUSES["mm" if kind.startswith("mm") else "vec"],
                )
            )
    rows += _finalist_compare(data_dir, plan)
    return rows, summarize(rows, data_dir, plan)


def _finalist_compare(data_dir: Path, plan: dict) -> list[dict]:
    path = data_dir / "migration_finalists.csv"
    if not path.is_file():
        return []
    old = {r["build"]: r for r in read_table(data_dir / "finalists_impl.csv")}
    rows = []
    for n in read_table(path):
        o, p = old[n["old"]], plan[n["old"]]
        b = n["old"]
        rows.append(
            _row(
                b,
                "det",
                "finalist",
                "rtl",
                "bit_exact",
                1,
                int(n["bit_exact"]),
                ok=n["bit_exact"] == "1",
            )
        )
        for k in COUNTERS:
            oc, nc = int(o[f"csynth_{k}"]), int(n[f"csynth_{k}"])
            oi, ni = int(o[f"impl_{k}"]), int(n[f"impl_{k}"])
            c_cause = CAUSES["csynth"] if oc != nc else ""
            i_cause = CAUSES["impl"] if oi != ni else ""
            if k in ("dsp", "bram"):
                pred = int(p[f"pred_{k}"])
                rows.append(
                    _row(
                        b,
                        "det",
                        "finalist",
                        "csynth",
                        k,
                        oc,
                        nc,
                        pred=pred,
                        ok=nc == pred,
                        cause=c_cause,
                    )
                )
                ok = abs(ni - oi) <= abs(nc - oc)
                rows.append(
                    _row(b, "det", "finalist", "impl", k, oi, ni, ok=ok, cause=i_cause)
                )
            else:
                rows.append(
                    _row(b, "det", "finalist", "csynth", k, oc, nc, cause=c_cause)
                )
                ok = abs(ni - oi) <= LUT_FF_PCT / 100 * oi
                rows.append(
                    _row(b, "det", "finalist", "impl", k, oi, ni, ok=ok, cause=i_cause)
                )
        for name, (om, nm) in _impl_modules(data_dir).get(b, {}).items():
            for k in COUNTERS:
                if om[k] != nm[k]:
                    rows.append(
                        _row(
                            b,
                            "det",
                            "finalist",
                            f"impl-module:{name}",
                            k,
                            om[k],
                            nm[k],
                            cause=CAUSES[_placed(name)],
                        )
                    )
        oj, nj = float(o["rtl_job"]), float(n["rtl_job"])
        rows.append(
            _row(
                b,
                "det",
                "finalist",
                "cycles",
                f"job_nit{o['nit']}",
                oj,
                nj,
                ok=abs(nj - oj) <= CYCLE_PCT / 100 * oj + T0_CYCLES,
                cause=CAUSES["t0"] + "; " + CAUSES["t_iter"],
            )
        )
        rows.append(
            _row(
                b,
                "det",
                "finalist",
                "impl",
                "cp_post_impl_ns",
                float(o["cp_post_impl_ns"]),
                float(n["cp_post_impl_ns"]),
                ok=n["timing_met"] == "1" and float(n["cp_post_impl_ns"]) <= CLOCK_NS,
                cause=_timing(o["cp_post_impl_ns"], n["cp_post_impl_ns"]),
            )
        )
    return rows


FINALIST_MODULES = "migration_finalists_modules.csv"


def finalist_module_rows() -> list[dict]:
    """Per finalist and implemented module, the old and the new row, from Vivado's hierarchical
    utilization reports (the build trees, local; :func:`examples.mimo_cg.hw.impl_check.
    parse_hierarchy`), the blocks and the cores under one name each (``mm``, ``vec``).
    """
    from examples.mimo_cg.hw import impl_check

    rename = {"CgMm": "mm", "SystolicCore": "mm", "CgVec": "vec", "CgVectorCore": "vec"}
    out = []
    for f in [r for r in read_list() if r["role"] == "finalist"]:
        got = {}
        for side, build in (("old", f["old"]), ("new", f["build"])):
            h = impl_check.parse_hierarchy(impl_check.hier_path(build).read_text())
            got[side] = {rename.get(m, m): v for m, v in h.items()}
        for name in sorted(set(got["old"]) | set(got["new"])):
            row = {"build": f["old"], "module": name}
            for side in ("old", "new"):
                v = got[side].get(name, dict.fromkeys(COUNTERS, 0))
                row |= {f"{side}_{k}": v[k] for k in COUNTERS}
            out.append(row)
    return out


def _impl_modules(data_dir: Path) -> dict:
    """``{finalist: {module: (old counters, new counters)}}`` from :data:`FINALIST_MODULES`."""
    path = Path(data_dir) / FINALIST_MODULES
    out: dict = {}
    for r in read_table(path) if path.is_file() else []:
        out.setdefault(r["build"], {})[r["module"]] = tuple(
            {k: int(r[f"{side}_{k}"]) for k in COUNTERS} for side in ("old", "new")
        )
    return out


def trigger(data_dir: Path = PAPER_DATA, plan: dict | None = None) -> list[dict]:
    """The brute-force trigger's rank correlations and finalist pairs; ``fires`` on each row."""
    data_dir = Path(data_dir)
    plan = plan or {r["old"]: r for r in read_list(data_dir / LIST.name)}
    old_b = {r["build"]: r for r in read_table(data_dir / "hw_builds.csv")}
    new_b = {
        old_label(r["build"]): r for r in read_table(data_dir / "migration_builds.csv")
    }
    old_m, new_m = (
        _modules(data_dir / f"{s}_modules.csv") for s in ("hw", "migration")
    )
    old_c, new_c = (_cycles(data_dir / f"{s}_cycles.csv") for s in ("hw", "migration"))
    dets = [b for b, p in plan.items() if p["top"] == "det" and p["role"] != "finalist"]
    out = []

    def rho(group, quantity, pairs):
        o, n = zip(*pairs, strict=True)
        r = spearman(o, n)
        out.append(
            {"group": group, "quantity": quantity, "n": len(pairs), "value": round(r, 6),
             "fires": int(r < RHO_MIN)}
        )  # fmt: skip

    def job_at_K(c, b):
        K = int(plan[b]["K"])
        return dict(c[b]["job_interval"])[K]

    for group, members in (
        ("detectors", dets),
        ("detectors K=4", [b for b in dets if plan[b]["K"] == "4"]),
    ):
        for k in COUNTERS:
            rho(group, k, [(float(old_b[b][k]), float(new_b[b][k])) for b in members])
        rho(
            group,
            "job_at_K",
            [(job_at_K(old_c, b), job_at_K(new_c, b)) for b in members],
        )
    for top, span in (("vec", "vec.iter"), ("mm", "mm.iter")):
        members = [b for b, p in plan.items() if p["top"] == top]
        blk = "mm" if top == "mm" else "vec"
        for k in COUNTERS:
            rho(
                f"{top} blocks",
                k,
                [
                    (old_m[b][(blk, "module")][k], new_m[b][(blk, "module")][k])
                    for b in members
                ],
            )
        rho(
            f"{top} blocks",
            span,
            [(old_c[b]["spans"][span], new_c[b]["spans"][span]) for b in members],
        )
    path = data_dir / "migration_finalists.csv"
    if path.is_file():
        old = {r["build"]: r for r in read_table(data_dir / "finalists_impl.csv")}
        new = {r["old"]: r for r in read_table(path)}
        for k in ("lut", "ff"):
            for a in old:
                for b in old:
                    if a >= b or old[a]["K"] != old[b]["K"]:
                        continue
                    oa, ob = float(old[a][f"impl_{k}"]), float(old[b][f"impl_{k}"])
                    na, nb = float(new[a][f"impl_{k}"]), float(new[b][f"impl_{k}"])
                    apart = abs(oa - ob) / min(oa, ob) * 100
                    swapped = (oa - ob) * (na - nb) < 0
                    out.append(
                        {"group": f"finalists K={old[a]['K']}", "quantity": f"impl_{k}: {old[a]['name']} / {old[b]['name']}",
                         "n": 2, "value": round(apart, 3), "fires": int(swapped and apart > PAIR_PCT)}
                    )  # fmt: skip
    return out


def summarize(rows: list[dict], data_dir: Path, plan: dict) -> dict:
    """Per criterion: how many rows were checked and how many passed; and the trigger."""
    checked = [r for r in rows if r["pass"] != ""]
    crit: dict = {}
    for r in checked:
        key = (
            r["role"] == "finalist",
            r["scope"].split(":")[0],
            r["quantity"].split("_nit")[0],
        )
        n, ok = crit.get(key, (0, 0))
        crit[key] = (n + 1, ok + int(r["pass"]))
    trig = trigger(data_dir, plan)
    return {
        "criteria": [
            {"finalist": int(f), "scope": s, "quantity": q, "n": n, "pass": ok}
            for (f, s, q), (n, ok) in sorted(crit.items())
        ],
        "trigger": trig,
        "fires": int(any(t["fires"] for t in trig)),
    }


def write_compare(data_dir: Path = PAPER_DATA, out_dir: Path = PAPER_DATA) -> dict:
    rows, summary = compare(data_dir)
    note = provenance(
        "migration_compare",
        tool=_tools(Path(data_dir)),
        part=B.PART,
        period_ns=B.PERIOD_NS,
    )
    out = {
        "migration_compare": Path(out_dir) / "migration_compare.csv",
        "migration_metrics": Path(out_dir) / "migration_metrics.csv",
    }
    write_table(out["migration_compare"], rows, note)
    metrics = [
        {"kind": "criterion", "name": f"{'finalist ' if c['finalist'] else ''}{c['scope']}:{c['quantity']}",
         "n": c["n"], "value": c["pass"], "fires": ""}
        for c in summary["criteria"]
    ] + [
        {"kind": "trigger", "name": f"{t['group']}: {t['quantity']}", "n": t["n"],
         "value": t["value"], "fires": t["fires"]}
        for t in summary["trigger"]
    ]  # fmt: skip
    metrics.append(
        {"kind": "trigger", "name": "fires", "n": len(summary["trigger"]), "value": "", "fires": summary["fires"]}
    )  # fmt: skip
    write_table(out["migration_metrics"], metrics, note)
    return out


# --- the command line -------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("list", help=f"write {LIST.name}")
    p.add_argument("--check", action="store_true", help="regenerate and compare")
    p = sub.add_parser("finalists", help="csynth, RTL and Vivado of the 12 (long)")
    p.add_argument("--shard", default="0/1")
    sub.add_parser(
        "finalists-table", help="write migration_finalists.csv and its module rows"
    )
    sub.add_parser("compare", help="write migration_compare.csv and _metrics.csv")
    args = ap.parse_args(argv)
    if args.cmd == "list":
        if args.check:
            import tempfile

            with tempfile.TemporaryDirectory() as tmp:
                again = write_list(Path(tmp) / LIST.name)
                same = again.read_bytes() == LIST.read_bytes()
            print(f"{LIST.name}: {'identical' if same else 'DIFFERENT'}")
            return 0 if same else 1
        path = write_list()
        print(f"wrote {path} ({len(read_list(path))} builds)")
        return 0
    if args.cmd == "finalists":
        i, n = (int(x) for x in args.shard.split("/"))
        rows = [r for r in read_list() if r["role"] == "finalist"]
        for row in rows[i::n]:
            print(build_finalist(row), flush=True)
        return 0
    if args.cmd == "finalists-table":
        print("wrote", write_finalists())
        return 0
    for name, path in write_compare().items():
        print(f"{name} -> {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
