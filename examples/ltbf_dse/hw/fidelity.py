"""fidelity.py -- decision fidelity on the brute-force builds (plan step 6.2, AC13).

``plans/ltbf_dse/ltbf_dse.md`` AC13 and the rule pre-registered with the build sets
(``space.DECISION_RULE``, SHA-256 in §15).  A *design*: per array, a brute-force part of each kind
sharing ``W`` and 64-bit words (the systolic array, the vector unit's lanes, the DMA) and a runtime
point (``t``, form; ``r`` 1); a *decision*: (array, target, loss budget, T_LT budget, cost measure).

* **Predicted**: the DSE's table (``dse.py``): the frozen models' resources, the job-level simulator's
  T_LT on the frozen per-job models.
* **Measured**: each part's own csynth resources (the queue's reports; the Resp hub and Shared Mem as
  on the predicted side), and T_LT on the simulator with every job's cycles anchored on XSI: the
  frozen per-job model scaled, per part kind and array (and lanes), by the median ratio of measured to
  predicted cycles over that size's gated builds.  (The rule names csynth's latency corrected by the
  anchors; a free-running part's csynth reports no job latency -- its tasks never return -- so the
  anchors carry it alone: plan §14.)

The model's pick is the least predicted cost among the designs predicted to meet both budgets; it is
right when its measured T_LT <= 1.02 x the budget and its measured cost <= 1.10 x the least measured
cost among the designs measured to meet both ("none feasible" on both sides is right; on one only,
wrong).  AC13: >= 90% right per cost measure.  Loss is the accuracy table's on both sides.

Writes ``paper_data/decision_fidelity.csv`` and ``decision_fidelity_metrics.csv``.

    python -m examples.ltbf_dse.hw.fidelity
"""

from __future__ import annotations

import csv
import dataclasses
import itertools
import json
from functools import cache

import numpy as np

from examples.ltbf_dse.hw import dse
from examples.ltbf_dse.hw import space as sp

RULE = sp.DECISION_RULE


@cache
def anchors() -> dict:
    """``{(kind, n_rx, L or 0): measured / predicted}``: the median over the gated fit and held-out
    builds' jobs (or Ingest symbols) of that kind and size."""
    from examples.ltbf_dse.hw import models as hm

    model = json.loads(hm.MODELS.read_text())
    ratios: dict = {}
    for role in ("fit", "heldout"):
        for r in hm.load_reports(role):
            if not r.get("ops"):
                continue
            kind, p = r["part"]["kind"], sp.Part(**r["part"])
            X, y = hm.job_rows(kind, [r])
            key = (kind, p.n_rx, p.L if kind == "vec" else 0)
            ratios.setdefault(key, []).extend(
                v / hm._predict(model["cycles"][kind], x)
                for x, v in zip(X, y, strict=True)
            )
    return {k: float(np.median(v)) for k, v in ratios.items()}


def anchor(kind: str, n_rx: int, L: int = 0) -> float:
    a = anchors()
    if (kind, n_rx, L) in a:
        return a[kind, n_rx, L]
    near = [v for (k, n, _l), v in a.items() if k == kind and n == n_rx]
    return float(np.median(near)) if near else 1.0


def _scaled(timing, n_rx: int, L: int):
    fs, fv, fi = anchor("sys", n_rx), anchor("vec", n_rx, L), anchor("ing", n_rx)

    def wrap(f, g):
        def job(gid, j):
            r = f(gid, j)
            return (r[0] * g, r[1] * g) if isinstance(r, tuple) else r * g

        return job

    return dataclasses.replace(timing, sys_job=wrap(timing.sys_job, fs), vec_job=wrap(timing.vec_job, fv),
                               symbol=(timing.symbol or 0.0) * fi, symbol_port=timing.symbol_port * fi)  # fmt: skip


def _lt_case_measured(args: tuple) -> dict:
    """``dse._lt_case`` with the anchored per-job cycles."""
    n, drop, sector, form, t = args
    from examples.ltbf_dse.hw import jobsim as js
    from examples.ltbf_dse.hw import models as hm
    from examples.ltbf_dse.hw import system as sy

    model = json.loads(hm.MODELS.read_text())
    out = {"n": n, "drop": drop, "sector": sector, "form": form, "t": t}
    try:
        inp = sy.inputs(dse._params(n, drop=drop, sector=sector, form=form, t=t))
    except Exception as e:  # noqa: BLE001 -- reported
        out["skip"] = f"{type(e).__name__}: {e}"[:200]
        return out
    if not inp.own:
        out["skip"] = "no own UE"
        return out
    for L in dse.LANES:
        p = dataclasses.replace(inp.p, L=L)
        cfg, timing = js.fitted(sy.LtbfSystem(dataclasses.replace(inp, p=p)), model)
        r = js.JobSim(
            dataclasses.replace(cfg, lt_only=True), _scaled(timing, n * n, L)
        ).run()
        out[f"L{L}"] = {"t_lt": r["t_lt"] * dse.F_HOST, **r["busy"]}
    return out


@cache
def _report(name: str) -> dict:
    from examples.ltbf_dse.hw import queue as hq

    return json.loads((hq.REPORTS / f"{name}.json").read_text())


def measured_cost(n: int, R: int, C: int, W: int, L: int, form: str) -> dict:
    nrx = n * n
    parts = (
        sp.sys_part(nrx, R=R, C=C, W=W),
        sp.vec_part(nrx, L=L, W=W),
        sp.ing_part(nrx),
    )
    res = {c: sum(_report(p.name)["resources"][c] for p in parts) + dse._hub().get(c, 0)
           for c in ("LUT", "FF", "DSP", "BRAM_18K", "URAM")}  # fmt: skip
    pred = dse.cost(n, R, C, W, L, form)
    # Shared Mem's storage as on the predicted side (the same memory, placed the same way).
    mem_parts = {c: pred["res"][c] - sum(_pred_part(p)[c] for p in parts) - dse._hub().get(c, 0)
                 for c in ("URAM", "BRAM_18K")}  # fmt: skip
    for c, v in mem_parts.items():
        res[c] += v
    frac = {c: res[c] / dse.AVAILABLE[c] for c in dse.AVAILABLE}
    frac["max"] = max(frac.values())
    return frac


def _pred_part(p: sp.Part) -> dict:
    from examples.ltbf_dse.hw import models as hm

    return hm.predict_resources(dse._model(), p)


def decide(rows: list[dict], cost_key, time_key) -> dict:
    """``{decision: design or None}`` on *rows* (each a design with its loss) by the rule."""
    out = {}
    for n, target, lb, tb, cm in itertools.product(dse.ARRAYS, RULE["targets"], RULE["loss_budgets_db"],
                                                   RULE["time_budgets_ms"], RULE["cost_measures"]):  # fmt: skip
        ok = [
            r
            for r in rows
            if r["n"] == n
            and r["target"] == target
            and r["loss"] <= lb
            and time_key(r) <= tb
        ]
        out[n, target, lb, tb, cm] = min(ok, key=lambda r: (cost_key(r)[cm], cost_key(r)["max"], time_key(r)),
                                         default=None)  # fmt: skip
    return out


def main() -> dict:
    lt_pred = dse.t_lt_table(_load_time("dse_time.csv"))
    cases = list(
        itertools.product(dse.ARRAYS, dse.DROPS, dse.SECTORS, dse.FORMS, dse.TS)
    )
    with dse._pool() as pool:
        lt_meas = dse.t_lt_table(pool.map(_lt_case_measured, cases, chunksize=1))
    rows = []
    for n, (R, C), W, L, form, t, target in itertools.product(dse.ARRAYS, dse.TILES, dse.WIDTHS, dse.LANES,
                                                              dse.FORMS, dse.TS, RULE["targets"]):  # fmt: skip
        lo = dse.loss(n, target, W, form, t)
        if lo is None or (n, L, form, t) not in lt_pred:
            continue
        rows.append({"n": n, "target": target, "R": R, "C": C, "W": W, "L": L, "form": form, "t": t,
                     "loss": lo[0], "pred_cost": dse.cost(n, R, C, W, L, form)["frac"],
                     "meas_cost": measured_cost(n, R, C, W, L, form),
                     "pred_ms": lt_pred[n, L, form, t]["t_lt"] / dse.F_HOST * 1e3,
                     "meas_ms": lt_meas[n, L, form, t]["t_lt"] / dse.F_HOST * 1e3})  # fmt: skip
    pick = decide(rows, lambda r: r["pred_cost"], lambda r: r["pred_ms"])
    best = decide(rows, lambda r: r["meas_cost"], lambda r: r["meas_ms"])
    out_rows, right = [], {c: [] for c in RULE["cost_measures"]}
    for key, p in pick.items():
        n, target, lb, tb, cm = key
        b = best[key]
        if p is None or b is None:
            ok = p is None and b is None
        else:
            ok = (
                p["meas_ms"] <= RULE["time_tolerance"] * tb
                and p["meas_cost"][cm] <= RULE["cost_tolerance"] * b["meas_cost"][cm]
            )
        right[cm].append(ok)
        out_rows.append({"array": f"{n}x{n}", "target": target, "loss_budget_db": lb, "time_budget_ms": tb,
                         "cost_measure": cm, "right": ok,
                         "pick": "" if p is None else f"{p['R']}x{p['C']} W{p['W']} L{p['L']} {p['form']} t{p['t']}",
                         "pick_meas_ms": "" if p is None else p["meas_ms"],
                         "pick_meas_cost": "" if p is None else p["meas_cost"][cm],
                         "best": "" if b is None else f"{b['R']}x{b['C']} W{b['W']} L{b['L']} {b['form']} t{b['t']}",
                         "best_meas_cost": "" if b is None else b["meas_cost"][cm]})  # fmt: skip
    metrics = {
        cm: {"right": sum(v), "decisions": len(v), "fraction": sum(v) / len(v)}
        for cm, v in right.items()
    }
    dse._write("decision_fidelity.csv", out_rows)
    dse._write(
        "decision_fidelity_metrics.csv",
        [{"cost_measure": cm, **m} for cm, m in metrics.items()],
    )
    return metrics


def _load_time(name: str) -> list[dict]:
    """``dse_time.csv`` back into ``dse._lt_case``'s shape."""
    out = []
    with (dse.PAPER / name).open() as fh:
        for r in csv.DictReader(fh):
            row = {"n": int(r["n"]), "drop": int(r["drop"]), "sector": int(r["sector"]), "form": r["form"],
                   "t": int(r["t"])}  # fmt: skip
            for L in ("L2", "L4"):
                if r.get(f"{L}_t_lt"):
                    row[L] = {
                        k: float(r[f"{L}_{k}"])
                        for k in ("t_lt", "sys", "vec", "port", "cpu")
                    }
            out.append(row)
    return out


if __name__ == "__main__":
    for cm, m in main().items():
        print(
            f"{cm:9} right {m['right']}/{m['decisions']} ({m['fraction']:.1%})  AC13: >= {RULE['pass_fraction']:.0%}"
        )
