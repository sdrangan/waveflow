"""dse.py — the design-space exploration: measured accuracy joined with predicted cost.

Step 6.2 of ``plans/mimo_cg/mimo_cg_paper_sims.md`` (gate 6.0 decision record, §14).

A **joint design** is a scenario (modulation, M, K), a hardware configuration of the space at that
K, and an iteration count.  Its SNR loss is *measured*: Phase 3's
``paper_data/accuracy_losses.csv``, where it depends on the scenario, W, g_s and nit only.  Its
resources and its job time are *predicted*: Phase 5's
:func:`~examples.mimo_cg.hw.estimate.estimate`, with no toolchain.  The space has 6,084,720 joint
designs (:func:`joint_designs`), and this module prices all of them in seconds.

What it computes
----------------
* **Candidates** (:func:`candidates`).  At a loss budget, a hardware configuration is a candidate of
  a scenario when its format reaches the budget, and it runs the fewest iterations that do: more
  iterations only cost time, and in fixed point they do not always help.
* **The memory guard.**  The job-time law ``T0 + nit·T_iter`` holds while the CG loop is the
  bottleneck.  A job also reads ``K² + K·N`` complex values from memory, one word per cycle
  (:func:`memory_floor`).  A candidate whose predicted job time is under :data:`GUARD` times that
  floor is ``guarded``: the models make no latency claim for it, so it is left out of the frontier
  and never picked.
* **Every joint design** (:func:`explore`) is priced: its loss, its job time and whether the
  guard flags it.  The per-scenario table counts them.
* **The frontier** (:func:`frontier`).  The candidates no other candidate beats on every one of
  DSP, LUT, FF, block RAM and job time.  The command-queue depth is held at
  :data:`FRONTIER_CMD_DEPTH`, because the models cannot resolve its effect (2 LUTs at most).
* **Decisions** (:func:`pick`).  The cheapest candidate, in one resource, that meets a job-time
  budget.  Step 6.5 asks the same question of the measured numbers and compares the answers.

The SNR loss enters as a budget, at the three levels of :data:`LOSS_BUDGETS_DB`.

::

    python -m examples.mimo_cg.hw.dse      # paper_data/dse_frontier.csv and dse_scenarios.csv
"""

from __future__ import annotations

import argparse
import time
from dataclasses import astuple
from pathlib import Path

import numpy as np
import pandas as pd

from examples.mimo_cg.hw import models as MD
from examples.mimo_cg.hw.common import DEFAULT_N, LANE_BITS
from examples.mimo_cg.hw.estimate import CLOCK_NS, estimate
from examples.mimo_cg.hw.space import NITS, HwConfig, full_space, label
from examples.mimo_cg.mimo_cg import provenance, read_table, write_table

HERE = Path(__file__).resolve().parent
PAPER_DATA = MD.PAPER_DATA
ACCURACY = PAPER_DATA / "accuracy_losses.csv"
KNOBS = tuple(HwConfig.__dataclass_fields__)
RESOURCES = ("dsp", "lut", "ff", "bram")
#: What a design is judged on, all to be minimised.
OBJECTIVES = (*RESOURCES, "job")
#: SNR-loss budgets against floating-point exact MMSE at BER 1e-3, in dB (gate 6.0, item 5).
LOSS_BUDGETS_DB = (0.25, 0.5, 1.0)
#: A design is loop-dominated when its predicted job time is at least this many memory floors.
GUARD = 2.0
#: The command-queue depth of every frontier design (its predicted effect is at most 2 LUTs).
FRONTIER_CMD_DEPTH = 2
#: The joint designs of the space: hardware configurations × iteration counts × scenarios.
JOINT_DESIGNS = 6_084_720


# --- the two sides ---------------------------------------------------------------------------


def accuracy(path: Path = ACCURACY) -> pd.DataFrame:
    """The accuracy rows the hardware can build: fixed-point CG in the recurrence form, with
    W within the memory lane.  ``ok`` is false where the curve never reaches the target BER.
    """
    rows = [
        {
            "modulation": r["modulation"],
            "M": int(r["M"]),
            "K": int(r["K"]),
            "W": int(r["W"]),
            "g_s": int(r["g_s"]),
            "nit": int(r["nit"]),
            "ok": r["status"] == "ok",
            "loss": float(r["loss_mmse_db"]) if r["status"] == "ok" else np.inf,
            "sigma": float(r["loss_sigma_db"]) if r["status"] == "ok" else np.nan,
        }
        for r in read_table(path)
        if r["residual"] == "recurrence" and r["W"] and int(r["W"]) <= LANE_BITS
    ]
    acc = pd.DataFrame(rows)
    for K, nits in NITS.items():
        assert tuple(sorted(acc[acc.K == K].nit.unique())) == nits, (K, nits)
    return acc


def scenarios(acc: pd.DataFrame) -> list[tuple[str, int, int]]:
    """Every (modulation, M, K) of the accuracy table, in a fixed order."""
    keys = acc[["modulation", "M", "K"]].drop_duplicates()
    return sorted((m, int(M), int(K)) for m, M, K in keys.itertuples(index=False))


def hw_table(models: MD.Models | None = None, configs=None) -> pd.DataFrame:
    """Predicted resources and job timing of ``configs`` (default: the whole space), one row
    each, in the given order (``order`` keeps it through joins)."""
    models = models or MD.calibrated()
    rows = []
    for c in full_space() if configs is None else configs:
        e = estimate(c, models)
        rows.append((*astuple(c), e.dsp, e.lut, e.ff, e.bram, e.t0, e.t_iter))
    hw = pd.DataFrame(rows, columns=[*KNOBS, *RESOURCES, "t0", "t_iter"])
    hw.insert(0, "order", np.arange(len(hw)))
    return hw


def memory_floor(K, mem_dw):
    """Cycles to read one job's ``A`` and ``B`` from memory: ``K² + K·N`` complex values of two
    16-bit lanes each, at one memory word per cycle."""
    return (K * K + K * DEFAULT_N) * 2 * LANE_BITS / mem_dw


def joint_designs(hw: pd.DataFrame, acc: pd.DataFrame) -> int:
    """How many joint designs there are: for each scenario, the configurations at its K times the
    iteration counts of that K."""
    per_k = hw.groupby("K").size()
    return sum(int(per_k[K]) * len(NITS[K]) for _m, _M, K in scenarios(acc))


# --- candidates ------------------------------------------------------------------------------


def reach(acc: pd.DataFrame, scenario: tuple, budget_db: float) -> pd.DataFrame:
    """Per format ``(W, g_s)`` that reaches ``budget_db`` in ``scenario``: the fewest iterations
    that do, with that design's loss and its Monte Carlo σ."""
    mod, M, K = scenario
    a = acc[(acc.modulation == mod) & (acc.M == M) & (acc.K == K)]
    a = a[a.ok & (a.loss <= budget_db)].sort_values(["W", "g_s", "nit"])
    return a.groupby(["W", "g_s"], as_index=False).first()[
        ["W", "g_s", "nit", "loss", "sigma"]
    ]


def candidates(
    hw: pd.DataFrame, acc: pd.DataFrame, scenario: tuple, budget_db: float
) -> pd.DataFrame:
    """The designs of ``hw`` that reach ``budget_db`` in ``scenario``, each at its fewest
    iterations, with the predicted job time and the memory guard.  In ``hw`` order."""
    K = scenario[2]
    j = hw[hw.K == K].merge(reach(acc, scenario, budget_db), on=["W", "g_s"])
    j = j.sort_values("order").reset_index(drop=True)
    j["job"] = j.t0 + j.nit * j.t_iter
    j["mem_ratio"] = j.job / memory_floor(K, j.mem_dw)
    j["guarded"] = j.mem_ratio < GUARD
    # within two σ of the budget: another Monte Carlo run could put it outside (as in Phase 3)
    j["fragile"] = j.loss + 2 * j.sigma > budget_db
    return j


# --- the frontier ----------------------------------------------------------------------------


def pareto(points: np.ndarray) -> np.ndarray:
    """Mask of the rows of ``points`` (every column minimised) that no other row beats.

    A row is dropped when another row is no worse in every column and better in one — or equal in
    every column and earlier, so one design stands for each distinct point.
    """
    points = np.asarray(points, dtype=float)
    order = np.lexsort((np.arange(len(points)), *points.T[::-1]))
    srt = points[order]
    keep = np.ones(len(srt), dtype=bool)
    for i in range(len(srt)):
        if keep[i]:
            # a later row in this order is never better in the first column that differs
            keep[i + 1 :][np.all(srt[i + 1 :] >= srt[i], axis=1)] = False
    mask = np.zeros(len(points), dtype=bool)
    mask[order[keep]] = True
    return mask


def frontier(cands: pd.DataFrame, objectives: tuple = OBJECTIVES) -> pd.DataFrame:
    """The candidates on the frontier of ``objectives``: not guarded, at the frontier's
    command-queue depth, and beaten by no other such candidate."""
    pool = cands[~cands.guarded & (cands.cmd_depth == FRONTIER_CMD_DEPTH)]
    return pool[pareto(pool[list(objectives)].to_numpy())]


# --- decisions -------------------------------------------------------------------------------


def pick(cost, job, job_budget: float, eligible=None) -> int | None:
    """The position of the cheapest design whose ``job`` meets ``job_budget``, among the
    ``eligible`` ones; ``None`` if there is none.  Ties go to the faster design, then the first.
    """
    cost, job = np.asarray(cost, dtype=float), np.asarray(job, dtype=float)
    ok = job <= job_budget
    if eligible is not None:
        ok &= np.asarray(eligible, dtype=bool)
    idx = np.flatnonzero(ok)
    if not len(idx):
        return None
    return int(idx[np.lexsort((idx, job[idx], cost[idx]))[0]])


# --- the tables ------------------------------------------------------------------------------


def _design(r) -> str:
    return f"{label('det', HwConfig(**{k: int(r[k]) for k in KNOBS}))}_n{int(r['nit'])}"


def explore(hw: pd.DataFrame, acc: pd.DataFrame) -> tuple[list[dict], list[dict]]:
    """Every scenario at every loss budget: ``(frontier rows, summary rows)``."""
    front_rows, summary = [], []
    for scn in scenarios(acc):
        mod, M, K = scn
        a = acc[(acc.modulation == mod) & (acc.M == M) & (acc.K == K)]
        # every joint design of the scenario: its loss, its job time, and the guard
        fmt = hw[hw.K == K].merge(a, on=["W", "g_s"])
        fmt_job = fmt.t0 + fmt.nit * fmt.t_iter
        fmt_bound = fmt_job < GUARD * memory_floor(K, fmt.mem_dw)
        for budget in LOSS_BUDGETS_DB:
            head = {"modulation": mod, "M": M, "K": K, "budget_db": budget}
            cands = candidates(hw, acc, scn, budget)
            front = frontier(cands)
            pool = cands[~cands.guarded & (cands.cmd_depth == FRONTIER_CMD_DEPTH)]
            two = {
                r: set(pool[pareto(pool[[r, "job"]].to_numpy())].order)
                for r in ("dsp", "lut")
            }
            for _, r in front.iterrows():
                front_rows.append(
                    head
                    | {k: int(r[k]) for k in ("W", "g_s", "nit")}
                    | {"loss_db": r["loss"], "fragile": int(r["fragile"])}
                    | {k: int(r[k]) for k in KNOBS if k not in ("K", "W", "g_s")}
                    | {k: int(r[k]) for k in RESOURCES}
                    | {
                        "job_cycles": round(float(r["job"]), 2),
                        "job_us": round(float(r["job"]) * CLOCK_NS * 1e-3, 4),
                        "mem_ratio": round(float(r["mem_ratio"]), 2),
                        "front_dsp_job": int(r["order"] in two["dsp"]),
                        "front_lut_job": int(r["order"] in two["lut"]),
                    }
                )
            row = head | {
                "joint_designs": len(fmt),
                "reach_target": int(fmt.ok.sum()),
                "memory_bound": int(fmt_bound.sum()),
                "within_budget": int((fmt.ok & (fmt.loss <= budget)).sum()),
                "candidates": len(cands),
                "guarded": int(cands.guarded.sum()),
                "frontier": len(front),
                "front_dsp_job": len(two["dsp"]),
                "front_lut_job": len(two["lut"]),
                "W_min": int(cands.W.min()),
                "nit_min": int(cands.nit.min()),
            }
            job = pool.job.to_numpy()
            for name, cost in (
                ("min_dsp", pool.dsp.to_numpy()),
                ("min_lut", pool.lut.to_numpy()),
                ("fastest", job),
            ):
                r = pool.iloc[pick(cost, job, np.inf)]
                row |= {
                    f"{name}": _design(r),
                    f"{name}_dsp": int(r["dsp"]),
                    f"{name}_lut": int(r["lut"]),
                    f"{name}_job_us": round(float(r["job"]) * CLOCK_NS * 1e-3, 4),
                }
            summary.append(row)
    return front_rows, summary


def write(out_dir: Path = PAPER_DATA, models: MD.Models | None = None) -> dict:
    """Run the exploration and write the two tables; returns ``{name: path}`` and the timing."""
    from examples.mimo_cg.hw.validate import hls_tool, model_sha256

    t = time.perf_counter()
    hw, acc = hw_table(models), accuracy()
    priced = time.perf_counter() - t
    n = joint_designs(hw, acc)
    assert n == JOINT_DESIGNS, n
    front, summary = explore(hw, acc)
    note = {
        "joint_designs": n,
        "model_sha256": model_sha256()[:16],
        "tool": hls_tool(),
        "part": MD.PART,
        "clock_ns": CLOCK_NS,
        "guard": GUARD,
    }
    out = {}
    for name, rows in (("dse_frontier", front), ("dse_scenarios", summary)):
        out[name] = Path(out_dir) / f"{name}.csv"
        write_table(out[name], rows, provenance(name, **note))
    out["seconds"] = {"pricing": priced, "total": time.perf_counter() - t}
    out["rows"] = {"dse_frontier": len(front), "dse_scenarios": len(summary)}
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, default=PAPER_DATA)
    args = ap.parse_args(argv)
    out = write(args.out)
    secs = out["seconds"]
    print(
        f"{JOINT_DESIGNS:,} joint designs; hardware priced in {secs['pricing']:.1f} s, "
        f"everything in {secs['total']:.1f} s"
    )
    for name in ("dse_frontier", "dse_scenarios"):
        print(f"  {out['rows'][name]:6d} rows -> {out[name]}")
    summary = read_table(out["dse_scenarios"])
    for budget in LOSS_BUDGETS_DB:
        rows = [r for r in summary if float(r["budget_db"]) == budget]
        size = [int(r["frontier"]) for r in rows]
        print(
            f"  {budget:4.2f} dB: {len(rows)} scenarios, frontier {min(size)}–{max(size)} designs, "
            f"{sum(int(r['guarded']) for r in rows)} candidates guarded"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
