"""fidelity.py — do the models make the decisions a brute force would make?

Steps 6.3 and 6.5 of ``plans/mimo_cg/mimo_cg_paper_sims.md`` (gate 6.0 decision record, item 5).

The brute-force sub-grid (:func:`examples.mimo_cg.hw.space.bruteforce_grid`) is a full
cross-product of 1,440 detectors, every one built and measured, so the best design in it for any
question is known.  The questions are **decisions**:

    in this scenario, with at most this SNR loss, which design of the sub-grid finishes a job
    within this many cycles for the least of this resource?

A decision is a scenario (modulation, M, K) × a loss budget × a job-time budget × a resource (DSP,
LUT, FF, block RAM).  The accuracy side is measured and shared, so both sides choose among the same
candidates, each at the fewest iterations that reach the loss budget.

* **The model's pick** uses predicted cost and predicted job time, and never takes a design the
  memory guard flags (:data:`examples.mimo_cg.hw.dse.GUARD`).
* **The brute-force pick** uses measured cost and measured job time, with no restriction.
* The model's pick is **right** when its measured job time does not exceed the budget by more than
  :data:`JOB_TOLERANCE` and its measured cost is within :data:`COST_TOLERANCE` of the brute-force
  pick's.  AC6 asks for at least :data:`MIN_RIGHT_PCT` percent right, for each resource.

The decision set — the job-time budgets and the model's pick for every decision — is written to
``paper_data/bruteforce_decisions.csv`` by ``--write-decisions`` and committed **before any
brute-force build runs** (step 6.3).  The budgets come from predicted job times only: per scenario
and loss budget, :data:`N_JOB_BUDGETS` values spaced geometrically from just above the fastest
eligible candidate to just above the slowest (:func:`job_budgets`).

::

    python -m examples.mimo_cg.hw.fidelity --write-decisions   # step 6.3, before the builds
    python -m examples.mimo_cg.hw.fidelity                     # step 6.5, after them
"""

from __future__ import annotations

import argparse
import math
from pathlib import Path

import numpy as np
import pandas as pd

from examples.mimo_cg.hw import dse
from examples.mimo_cg.hw import models as MD
from examples.mimo_cg.hw.space import bf_label, bruteforce_grid
from examples.mimo_cg.mimo_cg import provenance, read_table, write_table

PAPER_DATA = MD.PAPER_DATA
DECISIONS = PAPER_DATA / "bruteforce_decisions.csv"
#: Job-time budgets per scenario and loss budget.
N_JOB_BUDGETS = 8
#: The lowest budget is this much above the fastest eligible candidate, the highest this much
#: above the slowest, so no budget sits on a design.
BUDGET_MARGIN = 1.1
#: AC6 (gate 6.0, item 5): the pick may exceed the job-time budget by 2% when measured, and cost
#: up to 10% more than the brute-force pick; at least 90% of the decisions must be right.
JOB_TOLERANCE = 0.02
COST_TOLERANCE = 0.10
MIN_RIGHT_PCT = 90.0


# --- the decision set (step 6.3) --------------------------------------------------------------


def subgrid_table(models: MD.Models | None = None) -> pd.DataFrame:
    """The sub-grid with its predicted resources and job timing, one row per build, in grid
    order (``order`` is the row's position, ``build`` its brute-force label)."""
    grid = bruteforce_grid()
    hw = dse.hw_table(models, grid)
    hw.insert(1, "build", [bf_label(c) for c in grid])
    return hw


def job_budgets(cands: pd.DataFrame) -> list[int]:
    """The job-time budgets of one scenario and loss budget, in cycles: from just above the
    fastest candidate the model may pick to just above the slowest, geometrically spaced.
    """
    job = cands.job[~cands.guarded]
    lo, hi = BUDGET_MARGIN * job.min(), BUDGET_MARGIN * job.max()
    steps = N_JOB_BUDGETS - 1
    return [math.ceil(lo * (hi / lo) ** (i / steps)) for i in range(N_JOB_BUDGETS)]


def decisions(hw: pd.DataFrame, acc: pd.DataFrame) -> list[dict]:
    """Every decision, with the model's pick: one row per scenario, loss budget, job-time budget
    and resource.  ``problem`` numbers the distinct questions: two scenarios whose formats reach
    the loss budget at the same iteration counts ask the same thing of the hardware."""
    rows, problems = [], {}
    for scn in dse.scenarios(acc):
        mod, M, K = scn
        for budget in dse.LOSS_BUDGETS_DB:
            cands = dse.candidates(hw, acc, scn, budget)
            reach = tuple(sorted(set(zip(cands.W, cands.g_s, cands.nit, strict=True))))
            job, ok = cands.job.to_numpy(), ~cands.guarded.to_numpy()
            for tau in job_budgets(cands):
                for res in dse.RESOURCES:
                    i = dse.pick(cands[res].to_numpy(), job, tau, eligible=ok)
                    r = cands.iloc[i]
                    key = (K, reach, tau, res)
                    rows.append(
                        {
                            "modulation": mod,
                            "M": M,
                            "K": K,
                            "budget_db": budget,
                            "job_budget": tau,
                            "resource": res,
                            "problem": problems.setdefault(key, len(problems)),
                            "candidates": len(cands),
                            "eligible": int((ok & (job <= tau)).sum()),
                            "pick": r["build"],
                            "pick_nit": int(r["nit"]),
                            "pick_cost": int(r[res]),
                            "pick_job": round(float(r["job"]), 2),
                        }
                    )
    return rows


def write_decisions(path: Path = DECISIONS, models: MD.Models | None = None) -> Path:
    from examples.mimo_cg.hw.validate import model_sha256

    rows = decisions(subgrid_table(models), dse.accuracy())
    note = provenance(
        "bruteforce_decisions",
        model_sha256=model_sha256()[:16],
        guard=dse.GUARD,
        job_budgets=N_JOB_BUDGETS,
        margin=BUDGET_MARGIN,
    )
    write_table(path, rows, note)
    return path


# --- scoring (step 6.5) -----------------------------------------------------------------------


def measured_table(data_dir: Path = PAPER_DATA) -> pd.DataFrame:
    """The brute force as measured: one row per build with its csynth resources, and one column
    ``job<nit>`` per iteration count with the steady-state job time in cycles.  A build that
    failed has no row."""
    builds = [
        r
        for r in read_table(Path(data_dir) / "bruteforce_builds.csv")
        if not r["error"]
    ]
    out = pd.DataFrame(
        {"build": [r["build"] for r in builds]}
        | {k: [int(r[k]) for r in builds] for k in dse.RESOURCES}
    ).set_index("build")
    for r in read_table(Path(data_dir) / "bruteforce_cycles.csv"):
        if r["quantity"] == "job_time" and r["build"] in out.index:
            out.loc[r["build"], f"job{int(r['nit'])}"] = float(r["cycles"])
    return out


def with_measured(cands: pd.DataFrame, measured: pd.DataFrame) -> pd.DataFrame:
    """``cands`` with the measured cost of each (``m_<resource>``) and its measured job time at
    its own iteration count (``m_job``); NaN where the build was not measured."""
    out = cands.copy()
    got = measured.reindex(out.build)
    for res in dse.RESOURCES:
        out[f"m_{res}"] = got[res].to_numpy()
    out["m_job"] = [
        got.iloc[i].get(f"job{int(n)}", np.nan) for i, n in enumerate(out.nit)
    ]
    return out


def judge(cands: pd.DataFrame, res: str, tau: float) -> dict:
    """One decision, both ways.  ``cands`` carries predicted and measured columns
    (:func:`with_measured`).

    The model picks by predicted cost and job time among the candidates it may take; the brute
    force picks by measured cost and job time among all of them.  The model's pick is right when,
    measured, it is within :data:`JOB_TOLERANCE` of the budget and within :data:`COST_TOLERANCE`
    of the brute-force pick's cost.  When no design is measured to meet the budget there is no
    cost to compare, and the pick is right if it is within the job tolerance.
    """
    job, ok = cands.job.to_numpy(), ~cands.guarded.to_numpy()
    m_cost, m_job = cands[f"m_{res}"].to_numpy(float), cands.m_job.to_numpy(float)
    known = ~np.isnan(m_cost) & ~np.isnan(m_job)
    i = dse.pick(cands[res].to_numpy(), job, tau, eligible=ok)
    b = dse.pick(np.where(known, m_cost, np.inf), m_job, tau, eligible=known)
    out = {"pick": "", "bf_pick": "", "measured": 0, "met": 0, "right": 0}
    out |= {"same_build": 0, "same_cost": 0, "regret_pct": "", "void": 0}
    if b is not None:
        out |= {
            "bf_pick": cands.build.iloc[b],
            "bf_cost": int(m_cost[b]),
            "bf_job": int(m_job[b]),
        }
    if i is None:
        # nothing the model may take: void if the brute force finds nothing either (a budget
        # made for the whole sub-grid, asked of a part of it), otherwise a miss
        return out | {"void": int(b is None)}
    out["pick"] = cands.build.iloc[i]
    if not known[i]:
        return out  # the model's pick failed to build: not right, and reported as such
    met = bool(m_job[i] <= (1 + JOB_TOLERANCE) * tau)
    out |= {"measured": 1, "met": int(met)}
    out |= {"m_cost": int(m_cost[i]), "m_job": int(m_job[i])}
    if b is None:
        return out | {"right": int(met)}
    regret = (m_cost[i] - m_cost[b]) / m_cost[b] if m_cost[b] else float(m_cost[i] > 0)
    return out | {
        "regret_pct": round(100 * regret, 3),
        "right": int(met and regret <= COST_TOLERANCE),
        "same_build": int(i == b),
        "same_cost": int(met and m_cost[i] == m_cost[b]),
    }


def score(
    hw: pd.DataFrame,
    acc: pd.DataFrame,
    measured: pd.DataFrame,
    committed: list[dict] | None = None,
    exclude: frozenset = frozenset(),
    check: bool = True,
) -> list[dict]:
    """Judge every decision.  With ``committed`` (the rows of ``bruteforce_decisions.csv``) the
    job-time budgets are taken from there and the model's pick must be the committed one.  With
    ``exclude`` (build names) both sides choose from the sub-grid without those builds, under the
    same budgets.  ``check=False`` is for other models than the committed ones (the learning
    curve): the budgets are still the committed ones, the picks are whatever ``hw`` gives.
    """
    budgets: dict = {}
    for r in committed or decisions(hw, acc):
        key = (r["modulation"], int(r["M"]), int(r["K"]), float(r["budget_db"]))
        budgets.setdefault(key, {})[(int(r["job_budget"]), r["resource"])] = r
    rows = []
    for (mod, M, K, budget), asked in budgets.items():
        cands = with_measured(dse.candidates(hw, acc, (mod, M, K), budget), measured)
        cands = cands[~cands.build.isin(exclude)]
        for (tau, res), was in asked.items():
            got = judge(cands, res, tau)
            if check and committed is not None and not exclude:
                assert got["pick"] == was["pick"], (mod, M, K, budget, tau, res)
            rows.append(
                {
                    "modulation": mod,
                    "M": M,
                    "K": K,
                    "budget_db": budget,
                    "job_budget": tau,
                    "resource": res,
                    "problem": was["problem"],
                }
                | {k: got.get(k, "") for k in _JUDGED}
            )
    return rows


_JUDGED = (
    "pick",
    "m_cost",
    "m_job",
    "bf_pick",
    "bf_cost",
    "bf_job",
    "measured",
    "met",
    "regret_pct",
    "right",
    "same_build",
    "same_cost",
    "void",
)


def metrics(rows: list[dict], label: str = "all") -> list[dict]:
    """Per resource and over all four: the share of decisions that are right (AC6's gate, for the
    full sub-grid), and what is reported beside it."""
    out = []
    for res in (*dse.RESOURCES, "any"):
        mine = [r for r in rows if res in ("any", r["resource"]) and not r["void"]]
        if not mine:
            continue
        n = len(mine)
        regret = sorted(float(r["regret_pct"]) for r in mine if r["regret_pct"] != "")
        right = 100.0 * sum(r["right"] for r in mine) / n
        gate = label == "all" and res != "any"
        out.append(
            {
                "set": label,
                "resource": res,
                "decisions": n,
                "problems": len({r["problem"] for r in mine}),
                "right_pct": round(right, 2),
                "threshold": f">= {MIN_RIGHT_PCT:g}" if gate else "",
                "pass": int(right >= MIN_RIGHT_PCT) if gate else "",
                "same_build_pct": round(
                    100.0 * sum(r["same_build"] for r in mine) / n, 2
                ),
                "same_cost_pct": round(
                    100.0 * sum(r["same_cost"] for r in mine) / n, 2
                ),
                "missed_budget_pct": round(
                    100.0 * sum(r["measured"] and not r["met"] for r in mine) / n, 2
                ),
                "unmeasured": sum(not r["measured"] for r in mine),
                "regret_median_pct": (
                    round(float(np.median(regret)), 3) if regret else ""
                ),
                "regret_p95_pct": (
                    round(float(np.percentile(regret, 95)), 3) if regret else ""
                ),
                "regret_max_pct": round(regret[-1], 3) if regret else "",
                "regret_min_pct": round(regret[0], 3) if regret else "",
            }
        )
    return out


# --- beside the decisions: frontiers, errors, cost ---------------------------------------------


def frontier_overlap(
    hw: pd.DataFrame, acc: pd.DataFrame, measured: pd.DataFrame
) -> list[dict]:
    """Per scenario, loss budget and resource: the predicted frontier of (resource, job time)
    against the measured one.

    ``precision`` is the share of the predicted frontier that is on the measured frontier and
    ``recall`` the share of the measured frontier that was predicted — both exact, so two designs
    one LUT apart count as a miss.  ``covered`` is the share of the measured frontier that some
    predicted-frontier design matches within the decision tolerances, by its measured numbers.
    """
    rows = []
    for scn in dse.scenarios(acc):
        for budget in dse.LOSS_BUDGETS_DB:
            cands = with_measured(dse.candidates(hw, acc, scn, budget), measured)
            cands = cands[cands.m_job.notna()]
            pool = cands[~cands.guarded]
            for res in dse.RESOURCES:
                pred = pool[dse.pareto(pool[[res, "job"]].to_numpy())]
                meas = cands[dse.pareto(cands[[f"m_{res}", "m_job"]].to_numpy())]
                both = set(pred.build) & set(meas.build)
                p_cost = pred[f"m_{res}"].to_numpy(float)
                p_job = pred.m_job.to_numpy(float)
                covered = sum(
                    bool(
                        np.any(
                            (p_cost <= (1 + COST_TOLERANCE) * c)
                            & (p_job <= (1 + JOB_TOLERANCE) * j)
                        )
                    )
                    for c, j in zip(meas[f"m_{res}"], meas.m_job, strict=True)
                )
                rows.append(
                    {
                        "modulation": scn[0],
                        "M": scn[1],
                        "K": scn[2],
                        "budget_db": budget,
                        "resource": res,
                        "candidates": len(cands),
                        "predicted": len(pred),
                        "measured": len(meas),
                        "both": len(both),
                        "precision_pct": round(100.0 * len(both) / len(pred), 2),
                        "recall_pct": round(100.0 * len(both) / len(meas), 2),
                        "covered_pct": round(100.0 * covered / len(meas), 2),
                    }
                )
    return rows


def model_errors(hw: pd.DataFrame, measured: pd.DataFrame) -> list[dict]:
    """Prediction against measurement for every measured sub-grid build: one row per build and
    quantity (the four resources, and the job time at every iteration count of its K, with the
    guard's verdict for that job)."""
    from examples.mimo_cg.hw.space import NITS

    rows = []
    for r in hw.itertuples():
        if r.build not in measured.index:
            continue
        m = measured.loc[r.build]
        head = {"build": r.build, "K": r.K, "L": r.L, "R": r.R, "W": r.W}
        for res in dse.RESOURCES:
            pred, got = float(getattr(r, res)), float(m[res])
            rows.append(
                head | {"quantity": res, "nit": "", "guarded": ""} | _error(pred, got)
            )
        floor = dse.memory_floor(r.K, r.mem_dw)
        for nit in NITS[r.K]:
            pred, got = r.t0 + nit * r.t_iter, float(m[f"job{nit}"])
            rows.append(
                head
                | {
                    "quantity": "job",
                    "nit": nit,
                    "guarded": int(pred < dse.GUARD * floor),
                }
                | _error(pred, got)
            )
    return rows


def _error(pred: float, got: float) -> dict:
    return {
        "measured": got,
        "predicted": round(pred, 2),
        "error_pct": round(100.0 * (pred - got) / got, 3) if got else 0.0,
        "exact": int(round(pred) == round(got)),
    }


def error_metrics(errors: list[dict]) -> list[dict]:
    """The sub-grid's model errors in a few lines: the counted resources as exact counts, the
    fitted ones and the job time as mean and worst absolute percent."""
    out = []

    def add(metric: str, rows: list[dict], exact: bool = False) -> None:
        if not rows:
            return
        err = [abs(r["error_pct"]) for r in rows]
        out.append(
            {
                "metric": metric,
                "n": len(rows),
                "value": (
                    round(100.0 * sum(r["exact"] for r in rows) / len(rows), 3)
                    if exact
                    else round(sum(err) / len(err), 3)
                ),
                "worst": "" if exact else round(max(err), 3),
            }
        )

    by = {q: [r for r in errors if r["quantity"] == q] for q in (*dse.RESOURCES, "job")}
    add("DSP exact (%)", by["dsp"], exact=True)
    add("BRAM exact (%)", by["bram"], exact=True)
    add("LUT MAPE (%)", by["lut"])
    add("FF MAPE (%)", by["ff"])
    add("job time MAPE (%), loop-dominated", [r for r in by["job"] if not r["guarded"]])
    add("job time MAPE (%), under the guard", [r for r in by["job"] if r["guarded"]])
    for lanes in sorted({r["L"] for r in errors}):
        add(f"LUT MAPE (%), {lanes} lanes", [r for r in by["lut"] if r["L"] == lanes])
    for K in sorted({r["K"] for r in errors}):
        mine = [r for r in by["job"] if r["K"] == K and not r["guarded"]]
        add(f"job time MAPE (%), loop-dominated, K = {K}", mine)
    return out


def tool_seconds(path: Path, roles: tuple[str, ...] | None = None) -> tuple[int, float]:
    """``(builds, csynth + RTL seconds)`` of a builds table, for the given roles."""
    rows = [r for r in read_table(path) if roles is None or r["role"] in roles]
    secs = sum(float(r["csynth_s"] or 0) + float(r["xsi_s"] or 0) for r in rows)
    return len(rows), secs


def projected_full_space(hw_all: pd.DataFrame, data_dir: Path = PAPER_DATA) -> float:
    """Tool-seconds a brute force of the whole space would take: a line of seconds against
    predicted LUTs and simulated cycles, fitted on the sub-grid's measured builds and summed over
    every configuration.  A projection, labelled as one wherever it is quoted."""
    from examples.mimo_cg.hw import measure as M
    from examples.mimo_cg.hw.space import HwConfig

    sub = subgrid_table().set_index("build")
    rows = [
        r
        for r in read_table(Path(data_dir) / "bruteforce_builds.csv")
        if not r["error"]
    ]
    knobs = list(dse.KNOBS)

    def sim(frame: pd.DataFrame) -> np.ndarray:
        return np.array(
            [
                M.cycles_budget(
                    HwConfig(*(int(v) for v in k)), M.job_nits(int(k[0]), True)
                )
                for k in frame[knobs].to_numpy()
            ],
            dtype=float,
        )

    mine = sub.loc[[r["build"] for r in rows]]
    X = np.c_[np.ones(len(mine)), mine.lut.to_numpy(float), sim(mine)]
    y = np.array([float(r["csynth_s"]) + float(r["xsi_s"]) for r in rows])
    coef, *_ = np.linalg.lstsq(X, y, rcond=None)
    full = np.c_[np.ones(len(hw_all)), hw_all.lut.to_numpy(float), sim(hw_all)]
    return float((full @ coef).sum())


# --- the learning curve (step 6.6) -------------------------------------------------------------

#: Calibration builds per refit, besides all of them; and refits per size.
CURVE_SIZES = (10, 20, 30, 45)
CURVE_DRAWS = 20
#: Seed namespace of the learning curve's draws.
_CURVE_STREAM = 93
#: The two detectors every refit keeps: without a detector at each memory word width no design
#: of the sub-grid can be priced (the bus adapters and the streams are measured per width).
CURVE_CORE = (
    "det_k4_l4_r4_c4_m4_w12g8_d64_s2_q2",
    "det_k4_l4_r4_c4_m4_w12g8_d32_s2_q2",
)


def calibration_builds(data_dir: Path = PAPER_DATA) -> dict[str, list[str]]:
    """The calibration builds by top, in table order."""
    from examples.mimo_cg.hw.space import FIT_ROLES, TOPS

    out: dict = {top: [] for top in TOPS}
    for r in read_table(Path(data_dir) / "hw_builds.csv"):
        if r["role"] in FIT_ROLES:
            out[r["top"]].append(r["build"])
    return out


def curve_subset(n: int, draw: int, pool: dict[str, list[str]]) -> frozenset:
    """``n`` calibration builds for one refit: the two core detectors, and the rest drawn without
    replacement, with each top's share of the ``n`` in proportion to its share of the pool
    (largest remainders)."""
    from examples.mimo_cg.mimo_link import BASE_SEED

    total = sum(len(v) for v in pool.values())
    exact = {top: n * len(v) / total for top, v in pool.items()}
    count = {top: int(x) for top, x in exact.items()}
    by_rest = sorted(pool, key=lambda t: exact[t] - count[t], reverse=True)
    for top in by_rest[: n - sum(count.values())]:
        count[top] += 1
    assert count["det"] >= len(CURVE_CORE) and sum(count.values()) == n
    rng = np.random.default_rng(
        np.random.SeedSequence([BASE_SEED, _CURVE_STREAM, n, draw])
    )
    chosen = set(CURVE_CORE)
    for top, builds in pool.items():
        rest = sorted(b for b in builds if b not in chosen)
        want = count[top] - sum(b in chosen for b in builds)
        chosen |= {rest[i] for i in rng.choice(len(rest), size=want, replace=False)}
    return frozenset(chosen)


def learning_curve(data_dir: Path = PAPER_DATA) -> list[dict]:
    """Refit the models on subsets of the calibration builds and judge every refit on the measured
    sub-grid, with the committed job-time budgets: one row per subset size, draw and resource.
    The last rows (all builds, draw 0) are the committed models'."""
    from examples.mimo_cg.hw.space import NITS

    data_dir = Path(data_dir)
    acc, measured = dse.accuracy(), measured_table(data_dir)
    committed = read_table(DECISIONS)
    pool = calibration_builds(data_dir)
    everything = sum(len(v) for v in pool.values())
    # job times are compared where the committed models call the job loop-dominated
    ref = subgrid_table()
    loop = {
        (r.build, nit)
        for r in ref.itertuples()
        for nit in NITS[r.K]
        if r.t0 + nit * r.t_iter >= dse.GUARD * dse.memory_floor(r.K, r.mem_dw)
    }
    rows = []
    for n in (*CURVE_SIZES, everything):
        for draw in range(1 if n == everything else CURVE_DRAWS):
            subset = None if n == everything else curve_subset(n, draw, pool)
            models = MD.fit(data_dir, builds=subset, loo=False)
            hw = subgrid_table(models)
            errors = model_errors(hw, measured)
            mape = {}
            for q in ("lut", "ff", "job"):
                err = [
                    abs(e["error_pct"])
                    for e in errors
                    if e["quantity"] == q
                    and (q != "job" or (e["build"], e["nit"]) in loop)
                ]
                mape[q] = round(sum(err) / len(err), 3)
            judged = score(hw, acc, measured, committed, check=False)
            for m in metrics(judged):
                rows.append(
                    {
                        "builds": n,
                        "draw": draw,
                        "resource": m["resource"],
                        "decisions": m["decisions"],
                        "right_pct": m["right_pct"],
                        "same_build_pct": m["same_build_pct"],
                        "missed_budget_pct": m["missed_budget_pct"],
                        "regret_p95_pct": m["regret_p95_pct"],
                        "regret_max_pct": m["regret_max_pct"],
                        "lut_mape_pct": mape["lut"],
                        "ff_mape_pct": mape["ff"],
                        "job_mape_pct": mape["job"],
                    }
                )
    return rows


def write_learning_curve(
    data_dir: Path = PAPER_DATA, out_dir: Path = PAPER_DATA
) -> Path:
    from examples.mimo_cg.hw.validate import hls_tool

    path = Path(out_dir) / "learning_curve.csv"
    note = provenance(
        "learning_curve",
        tool=hls_tool(data_dir),
        stream=_CURVE_STREAM,
        draws=CURVE_DRAWS,
        core="+".join(CURVE_CORE),
    )
    write_table(path, learning_curve(data_dir), note)
    return path


RUN_LOG = PAPER_DATA / "run_log.csv"


def cost_rows(data_dir: Path = PAPER_DATA) -> list[dict]:
    """What each part cost: builds and tool-hours from the builds tables, wall time from
    ``run_log.csv`` (measured when each campaign ran, plan §15), and the projection for a brute
    force of the whole space."""
    data_dir = Path(data_dir)
    wall = {}
    for r in read_table(data_dir / "run_log.csv"):
        wall.setdefault(r["part"], []).append(r)
    hw_all = dse.hw_table()
    parts = (
        ("calibration", "hw_builds.csv", ("fit", "fit2")),
        (
            "held-out validation",
            "hw_builds.csv",
            ("holdout", "supplement", "supplement2"),
        ),
        ("brute-force sub-grid", "bruteforce_builds.csv", None),
    )
    rows = []
    for part, table, roles in parts:
        n, secs = tool_seconds(data_dir / table, roles)
        runs = wall.get(part, [])
        rows.append(
            {
                "part": part,
                "builds": n,
                "designs": n,
                "tool_hours": round(secs / 3600, 3),
                "wall_hours": round(sum(float(r["wall_s"]) for r in runs) / 3600, 3),
                "processes": "/".join(sorted({r["processes"] for r in runs})),
                "kind": "measured",
            }
        )
    (py,) = wall["design-space exploration in Python"]
    rows.append(
        {
            "part": "design-space exploration in Python",
            "builds": 0,
            "designs": dse.JOINT_DESIGNS,
            "tool_hours": 0,
            "wall_hours": round(float(py["wall_s"]) / 3600, 5),
            "processes": py["processes"],
            "kind": "measured",
        }
    )
    sub = rows[2]
    full = projected_full_space(hw_all, data_dir)
    rows.append(
        {
            "part": "brute force of the whole space",
            "builds": len(hw_all),
            "designs": dse.JOINT_DESIGNS,
            "tool_hours": round(full / 3600, 1),
            # at the sub-grid run's own ratio of wall time to tool time
            "wall_hours": round(full / 3600 * sub["wall_hours"] / sub["tool_hours"], 1),
            "processes": sub["processes"],
            "kind": "projected",
        }
    )
    return rows


def write_scores(data_dir: Path = PAPER_DATA, out_dir: Path = PAPER_DATA) -> dict:
    """Score the committed decision set against the measured sub-grid and write the tables of
    step 6.5; returns ``{name: path}``."""
    from examples.mimo_cg.hw.space import BRUTEFORCE_PATH
    from examples.mimo_cg.hw.validate import hls_tool, model_sha256

    hw, acc = subgrid_table(), dse.accuracy()
    measured = measured_table(data_dir)
    committed = read_table(DECISIONS)
    rows = score(hw, acc, measured, committed)

    grid = read_table(BRUTEFORCE_PATH)
    in_fit = frozenset(r["build"] for r in grid if r["in_fit"] == "1")
    shares = frozenset(
        r["build"] for r in grid if r["vec_in_fit"] == "1" or r["mm_in_fit"] == "1"
    )
    first: dict = {}
    for r in rows:
        first.setdefault(r["problem"], r)
    table = metrics(rows)
    table += metrics(list(first.values()), "distinct questions")
    table += metrics(
        score(hw, acc, measured, committed, exclude=in_fit),
        "without the calibration detectors",
    )
    table += metrics(
        score(hw, acc, measured, committed, exclude=shares),
        "without builds that share a calibrated block",
    )
    errors = model_errors(hw, measured)
    note = {
        "tool": hls_tool(data_dir),
        "model_sha256": model_sha256()[:16],
        "measured_builds": len(measured),
        "job_tolerance": JOB_TOLERANCE,
        "cost_tolerance": COST_TOLERANCE,
    }
    out = {}
    for name, body in (
        ("decision_fidelity", rows),
        ("decision_fidelity_metrics", table),
        ("bruteforce_frontiers", frontier_overlap(hw, acc, measured)),
        ("bruteforce_errors", errors),
        ("bruteforce_error_metrics", error_metrics(errors)),
        ("dse_cost", cost_rows(data_dir)),
    ):
        out[name] = Path(out_dir) / f"{name}.csv"
        write_table(out[name], body, provenance(name, **note))
    return out


IMAGES = (
    Path(__file__).resolve().parents[3] / "docs" / "examples" / "mimo_cg" / "images"
)


def _typed(rows: list[dict]) -> list[dict]:
    """Rows read back from a table, with the flags the figures test as integers."""
    for r in rows:
        for k in ("measured", "right", "builds", "draw"):
            if k in r and r[k] != "":
                r[k] = int(float(r[k]))
    return rows


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--write-decisions", action="store_true")
    ap.add_argument("--learning-curve", action="store_true", help="step 6.6")
    ap.add_argument("--no-figure", action="store_true")
    args = ap.parse_args(argv)
    if args.learning_curve:
        path = write_learning_curve()
        rows = [r for r in read_table(path) if r["resource"] == "any"]
        for n in sorted({int(r["builds"]) for r in rows}):
            got = sorted(float(r["right_pct"]) for r in rows if int(r["builds"]) == n)
            print(
                f"  {n:3d} builds: decisions right {got[0]:6.2f}% … {got[-1]:6.2f}% "
                f"(median {got[len(got) // 2]:6.2f}%, {len(got)} refits)"
            )
        print("wrote", path)
        if not args.no_figure:
            from examples.mimo_cg.hw.fidelity_figure import render_curve

            print(
                "wrote",
                render_curve(_typed(read_table(path)), IMAGES / "learning_curve.svg"),
            )
        return 0
    if args.write_decisions:
        path = write_decisions()
        rows = read_table(path)
        picks = {r["pick"] for r in rows}
        print(
            f"{len(rows)} decisions ({len({r['problem'] for r in rows})} distinct), "
            f"{len(picks)} distinct picks -> {path}"
        )
        return 0
    out = write_scores()
    for name, path in out.items():
        print(f"{name} -> {path}")
    for m in read_table(out["decision_fidelity_metrics"]):
        gate = (
            f"   [{m['threshold']}: {'PASS' if m['pass'] == '1' else 'FAIL'}]"
            if m["threshold"]
            else ""
        )
        print(
            f"  {m['set']:44s} {m['resource']:5s} n={int(m['decisions']):5d}  right "
            f"{float(m['right_pct']):6.2f}%  same design {float(m['same_build_pct']):6.2f}%  "
            f"worst regret {m['regret_max_pct']}%{gate}"
        )
    if not args.no_figure:
        from examples.mimo_cg.hw.fidelity_figure import render_decisions

        rows = _typed(read_table(out["decision_fidelity"]))
        table = read_table(out["decision_fidelity_metrics"])
        print("wrote", render_decisions(rows, table, IMAGES / "decision_fidelity.svg"))
    gates = [m for m in read_table(out["decision_fidelity_metrics"]) if m["threshold"]]
    return 0 if all(m["pass"] == "1" for m in gates) else 1


if __name__ == "__main__":
    raise SystemExit(main())
