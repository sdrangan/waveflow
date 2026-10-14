"""fidelity.py -- does the DSE pick the design brute force would?  (step 5.3, AC9)

The pre-registered rule (``hw/prereg/rule.json``, hash in the plan's §15): over the brute-force
builds (``hw/prereg/brute.json``) run with every registered runtime choice, for each (array,
scenario, loss budget, time budget, cost measure) the **model's pick** is the least *predicted* cost
among designs *predicted* to meet both budgets; it is **right** when its *measured* time is within
1.02 x the time budget and its *measured* cost within 1.10 x the least measured cost among designs
*measured* to meet both budgets (both sides finding none is right; one side alone is wrong).
Pass: >= 90 % right per cost measure.

Measured: a build's csynth resources, and its time from its own csynth loop table (the structural
count, less the per-tile RTL correction).  Predicted: the frozen models (``hw/models.json``) from the
knobs.  Accuracy is the same for both (the sweep).  The rule names no placement of the
eigendecomposition: the registered score uses the calibrated one (``a53``); ``pl`` is reported too.

    python -m examples.ltbf_cg.hw.fidelity
"""

from __future__ import annotations

import itertools
import json
import math
import sys

import numpy as np
import pandas as pd  # type: ignore[import-untyped]

from examples.ltbf_cg.hw import dse
from examples.ltbf_cg.hw import models as md
from examples.ltbf_cg.hw import space as sp
from examples.ltbf_cg.hw import timing as tm

SIDE = {64: 8, 256: 16, 1024: 32}


def _times(k, o, regime, units, eig, drops, delta) -> np.ndarray:
    """p90 over drops of the update time (ms), per t."""
    memo: dict = {}
    vals = []
    for sectors in drops:
        cyc = []
        for pop in sectors:
            if pop not in memo:
                memo[pop] = dse._sector_cycles(k, o, pop[0], pop[1], regime, delta)
            c0, c1 = memo[pop]
            cyc.append(c0 + c1 * dse.T_ALL)
        eng = np.array(cyc)
        n_ue = sum(no for _, no in sectors)
        if eig == "pl":
            eng = (
                eng
                + np.array(
                    [no * tm.pl_eig_cycles(k, o, k.nmax, 1) for _, no in sectors]
                )[:, None]
            )
        a53 = (
            math.ceil(n_ue / tm.A53_CORES)
            * tm.a53_ue_cycles(k.nmax, 1, eig)
            / tm.A53_HZ
        )
        vals.append(dse._assign(eng, units) * tm.CLOCK_NS * 1e-9 + a53)
    return np.percentile(np.array(vals), 90, axis=0) * 1e3


def table(
    rule: dict, acc: pd.DataFrame, params: dict, delta: float, eig: str
) -> pd.DataFrame:
    rows = []
    brute = []
    for k in sp.load("brute"):
        p = md.REPORTS / f"{k.name}.json"
        if p.is_file() and (rec := json.loads(p.read_text())).get("ok"):
            brute.append((k, rec))
    for scen in rule["scenarios"]:
        for k, rec in brute:
            side = SIDE[k.nmax]
            drops = tm.sector_counts(scen["band"], scen["target"], k.nmax)
            a = acc[(acc.band == scen["band"]) & (acc.target == scen["target"]) & (acc.srs == scen["srs"])
                    & (acc.n == side) & (acc.kind == "fixed") & (acc.r == 1) & (acc.nue == 10)]  # fmt: skip
            a = a.assign(regime=a.regime.astype(int), t=a.t.astype(int)).set_index(
                ["format", "form", "regime", "t"]
            )
            o_meas = md.overheads_from_loops(k, rec["loops"])
            o_pred = md.predict_overheads(params, k)
            res_pred = md.predict_resources(params, k)
            res_meas = {
                r: float(rec["resources"][r]) for r in ("DSP", "LUT", "FF", "BRAM_18K")
            }
            form = "explicit" if k.explicit else "factored"
            for regime, units in itertools.product(
                rule["runtime_axes"]["regime"], rule["runtime_axes"]["units"]
            ):
                tp = _times(k, o_pred, regime, units, eig, drops, delta)
                tmeas = _times(k, o_meas, regime, units, eig, drops, delta)
                for t in rule["runtime_axes"]["t"]:
                    key = (f"W{k.W}g1", form, regime, t)
                    if key not in a.index:
                        continue
                    lm, lp, _ = a.loc[key][
                        ["median_loss_db", "p90_loss_db", "median_sinr_db"]
                    ]
                    r = {"scenario": f"{scen['band']}|{scen['target']}|{scen['srs']}", "side": side,
                         "build": k.name, "regime": regime, "units": units, "t": t,
                         "loss_med": lm, "loss_p90": lp, "time_pred": tp[t - 1], "time_meas": tmeas[t - 1]}  # fmt: skip
                    for rs in ("DSP", "LUT", "FF", "BRAM_18K"):
                        r[f"{rs}_pred"] = res_pred[rs] * units / dse.AVAILABLE[rs]
                        r[f"{rs}_meas"] = res_meas[rs] * units / dse.AVAILABLE[rs]
                    for side_ in ("pred", "meas"):
                        r[f"max_{side_}"] = max(
                            r[f"{rs}_{side_}"]
                            for rs in ("DSP", "LUT", "FF", "BRAM_18K")
                        )
                    rows.append(r)
    return pd.DataFrame(rows)


def score(df: pd.DataFrame, rule: dict) -> pd.DataFrame:
    out = []
    for (scen, side), g in df.groupby(["scenario", "side"]):
        for lb, tb, cm in itertools.product(
            rule["loss_budgets_db"], rule["time_budgets_ms"], rule["cost_measures"]
        ):
            c = "max" if cm == "max" else cm
            lossok = (g.loss_med <= lb) & (g.loss_p90 <= lb)
            pf = g[lossok & (g.time_pred <= tb) & (g["max_pred"] <= 1.0)]
            mf = g[lossok & (g.time_meas <= tb) & (g["max_meas"] <= 1.0)]
            if pf.empty or mf.empty:
                right = pf.empty and mf.empty
                pick = None
            else:
                pick = pf.loc[pf[f"{c}_pred"].idxmin()]
                best = mf[f"{c}_meas"].min()
                right = bool(
                    pick.time_meas <= rule["time_tolerance"] * tb
                    and pick[f"{c}_meas"] <= rule["cost_tolerance"] * best + 1e-12
                )
            out.append({"scenario": scen, "side": side, "loss_budget_db": lb, "time_budget_ms": tb,
                        "cost": cm, "n_pred_ok": len(pf), "n_meas_ok": len(mf), "right": right,
                        "pick": None if pick is None else f"{pick.build}|r{pick.regime}|u{pick.units}|t{pick.t}"})  # fmt: skip
    return pd.DataFrame(out)


def main() -> int:
    rule = json.loads((sp.PREREG / "rule.json").read_text())
    acc = pd.read_csv(dse.PAPER / "accuracy_losses.csv")
    params = md.load()
    delta = json.loads((sp.PREREG.parent / "reports" / "rtl_fit.json").read_text())[
        "delta"
    ]
    ok = True
    for eig in ("a53", "pl"):
        df = table(rule, acc, params, delta, eig)
        sc = score(df, rule)
        sc.to_csv(dse.PAPER / f"fidelity_{eig}.csv", index=False)
        frac = sc.groupby("cost")["right"].mean()
        print(
            f"eig={eig}: {len(df)} designs, {len(sc)} decisions;",
            frac.round(3).to_dict(),
        )
        if eig == "a53":
            ok = bool((frac >= rule["pass_fraction"]).all())
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
