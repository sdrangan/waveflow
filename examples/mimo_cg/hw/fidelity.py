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
    out |= {"same_build": 0, "same_cost": 0, "regret_pct": ""}
    if b is not None:
        out |= {
            "bf_pick": cands.build.iloc[b],
            "bf_cost": int(m_cost[b]),
            "bf_job": int(m_job[b]),
        }
    if i is None:
        return out
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
) -> list[dict]:
    """Judge every decision.  With ``committed`` (the rows of ``bruteforce_decisions.csv``) the
    job-time budgets are taken from there and the model's pick must be the committed one.  With
    ``exclude`` (build names) both sides choose from the sub-grid without those builds, under the
    same budgets."""
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
            if committed is not None and not exclude:
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
)


def metrics(rows: list[dict], label: str = "all") -> list[dict]:
    """Per resource and over all four: the share of decisions that are right (AC6's gate, for the
    full sub-grid), and what is reported beside it."""
    out = []
    for res in (*dse.RESOURCES, "any"):
        mine = [r for r in rows if res in ("any", r["resource"])]
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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--write-decisions", action="store_true")
    args = ap.parse_args(argv)
    if args.write_decisions:
        path = write_decisions()
        rows = read_table(path)
        picks = {r["pick"] for r in rows}
        print(
            f"{len(rows)} decisions ({len({r['problem'] for r in rows})} distinct), "
            f"{len(picks)} distinct picks -> {path}"
        )
        return 0
    raise SystemExit("the tables of step 6.5 are written once the brute force has run")


if __name__ == "__main__":
    raise SystemExit(main())
