"""dse.py -- the design-space exploration: accuracy x time x cost of every LTBF CG design.

``plans/mimo_cg/ltbf_cg_dse.md`` step 5.2 (AC8).  A design is an engine build (form, R x C, W,
grouping = ``mmax`` 8 per-UE or 80 joint; ``cmax`` 240) run with ``t`` iterations in a regime, on
``units`` engines, with the eigendecomposition step on the A53 or (modeled) partly on the engine.
For each (band, scenario, array):

* **loss** -- the accuracy sweep's per-UE loss statistics (``paper_data/accuracy_losses.csv``) of
  the design's format, form, regime and ``t`` (r = 1);
* **time** -- one long-term update of all three sectors (:mod:`examples.ltbf_cg.hw.timing`) on each
  drop's sector populations; the 90th percentile over the drops;
* **cost** -- the predicted resources (:mod:`examples.ltbf_cg.hw.models`) times ``units``, as
  fractions of the xczu48dr; ``max`` is the largest fraction; a design whose fraction exceeds 1 is
  infeasible.  The explicit engine at 1024 keeps Q in DDR (modeled): no Q BRAM, and each iteration
  is bound by streaming Q (``ceil(m/C)`` passes of ``2 W N^2`` bits at :data:`DDR_BPS`).

Writes ``paper_data/dse_designs.csv.gz``, ``dse_decisions.csv`` (the cheapest design per loss budget,
time budget and cost measure) and ``dse_frontiers.csv``.

    python -m examples.ltbf_cg.hw.dse
"""

from __future__ import annotations

import itertools
import json
import math
import sys
from functools import cache

import numpy as np
import pandas as pd  # type: ignore[import-untyped]

from examples.ltbf_cg.hw import models as md
from examples.ltbf_cg.hw import space as sp
from examples.ltbf_cg.hw import timing as tm

PAPER = md.eg.HERE.parent / "paper_data"
#: xczu48dr-ffvg1517-2-e (the csynth reports' AvailableResources).
AVAILABLE = {"DSP": 4272, "LUT": 425280, "FF": 850560, "BRAM_18K": 2160}
COSTS = ("DSP", "LUT", "FF", "BRAM_18K", "max")
#: DDR4-2400 x 64 at 70 % efficiency (the RFSoC 4x2's PS DDR), bits per second.
DDR_BPS = 2400e6 * 64 * 0.7
ARRAYS = {8: 64, 16: 256, 32: 1024}
WIDTHS = (10, 12, 14, 16, 18, 20, 24)
GROUPING = {"per_ue": 8, "joint": 80}
UNITS = (1, 2, 3)
EIG = ("a53", "pl")
T_ALL = np.arange(1, 11)
LOSS_BUDGETS = (0.1, 0.5, 1.0)
TIME_BUDGETS_MS = (1.0, 5.0, 10.0)


def knob_grid(nmax: int) -> list[sp.Knobs]:
    out = []
    for explicit, (r, c), w, m in itertools.product(
        (False, True), itertools.product(sp.TILES, sp.TILES), WIDTHS, GROUPING.values()
    ):
        k = sp.Knobs(explicit, nmax, 240, m, r, c, w)
        ddr = explicit and nmax > sp.EXPLICIT_NMAX
        if (
            k.valid() or (ddr and r == c and not (nmax % r or 240 % r or m % c))
        ) and c <= m:
            out.append(k)
    return out


@cache
def params() -> dict:
    return md.load()


def cost(k: sp.Knobs) -> dict[str, float]:
    p = params()
    if k.explicit and k.nmax > sp.EXPLICIT_NMAX:  # Q in DDR: no Q storage on chip
        f = md.features(k)
        f["q_bits"] = 0.0
        res = {r: max(0.0, md._apply(q, f)) for r, q in p["resources"].items()}
    else:
        res = md.predict_resources(p, k)
    return res


def _sector_cycles(
    k: sp.Knobs, o: md.Overheads, n_conn: int, n_own: int, regime: int, delta: float
):
    """(fixed, per-iteration) engine cycles of one sector, and the DDR-bound per-iteration cycles."""
    if n_own == 0:
        return 0.0, 0.0
    n, cols = k.nmax, 8 * n_conn
    m_total = 8 * n_own if regime == 1 else n
    c0 = c1 = 0.0
    first = True
    for j in range(math.ceil(m_total / k.mmax)):
        m = min(k.mmax, m_total - j * k.mmax)
        a = md.job_cycles_rtl(k, o, n, cols, m, 0, first, delta)
        b = md.job_cycles_rtl(k, o, n, cols, m, 1, first, delta) - a
        if k.explicit and n > sp.EXPLICIT_NMAX:  # each S pass streams Q from DDR
            ddr = math.ceil(m / k.C) * 2 * k.W * n * n / DDR_BPS / (tm.CLOCK_NS * 1e-9)
            b = max(b, ddr)
        c0, c1 = c0 + a, c1 + b
        first = False
    return c0, c1


def _assign(cyc: np.ndarray, units: int) -> np.ndarray:
    """Longest-first assignment of the three sectors to ``units`` engines, per t: the makespan."""
    s = -np.sort(-cyc, axis=0)  # sectors sorted by load, per t
    if units >= 3:
        return s[0]
    if units == 2:
        return np.maximum(s[0], s[1] + s[2])
    return s.sum(axis=0)


def build_table(
    band: str, target: str, srs: str, side: int, acc: pd.DataFrame, delta: float
) -> pd.DataFrame:
    nmax = ARRAYS[side]
    drops = tm.sector_counts(band, target, nmax)
    a = acc[(acc.band == band) & (acc.target == target) & (acc.srs == srs) & (acc.n == side)
            & (acc.kind == "fixed") & (acc.r == 1) & (acc.nue == 10)]  # fmt: skip
    a = a.assign(regime=a.regime.astype(int), t=a.t.astype(int))
    a = a.set_index(["format", "form", "regime", "t"])[
        ["median_loss_db", "p90_loss_db", "median_sinr_db"]
    ]
    rows = []
    p = params()
    for k in knob_grid(nmax):
        o = md.predict_overheads(p, k)
        res = cost(k)
        form = "explicit" if k.explicit else "factored"
        for regime in (1, 2):
            # engine cycles per drop and sector, per t (memoized over the sector populations)
            memo: dict = {}
            per_drop = []
            for sectors in drops:
                for pop in sectors:
                    if pop not in memo:
                        memo[pop] = _sector_cycles(k, o, pop[0], pop[1], regime, delta)
                cyc = np.array([memo[pop] for pop in sectors])
                pl = np.array(
                    [no * tm.pl_eig_cycles(k, o, nmax, 1) for _, no in sectors]
                )
                n_ue = sum(no for _, no in sectors)
                per_drop.append((cyc, pl, n_ue))
            for units, eig in itertools.product(UNITS, EIG):
                t_drop = []
                for cyc, pl, n_ue in per_drop:
                    eng = cyc[:, :1] + cyc[:, 1:] * T_ALL[None, :]
                    if eig == "pl":
                        eng = eng + pl[:, None]
                    eng_s = _assign(eng, units) * tm.CLOCK_NS * 1e-9
                    a53_s = (
                        math.ceil(n_ue / tm.A53_CORES)
                        * tm.a53_ue_cycles(nmax, 1, eig)
                        / tm.A53_HZ
                    )
                    t_drop.append(eng_s + a53_s)
                t90 = np.percentile(np.array(t_drop), 90, axis=0) * 1e3
                for t in T_ALL:
                    key = (f"W{k.W}g1", form, regime, int(t))
                    if key not in a.index:
                        continue
                    lm, lp, sinr = a.loc[key]
                    r = {
                        "band": band, "target": target, "srs": srs, "side": side, "form": form,
                        "R": k.R, "C": k.C, "W": k.W, "grouping": "per_ue" if k.mmax == 8 else "joint",
                        "units": units, "eig": eig, "regime": regime, "t": int(t),
                        "loss_med": lm, "loss_p90": lp, "sinr_med": sinr, "time_p90_ms": float(t90[t - 1]),
                        "ddr_q": bool(k.explicit and nmax > sp.EXPLICIT_NMAX),
                    }  # fmt: skip
                    for rs in ("DSP", "LUT", "FF", "BRAM_18K"):
                        r[rs] = res[rs] * units
                        r[f"{rs}_frac"] = r[rs] / AVAILABLE[rs]
                    r["max_frac"] = max(
                        r[f"{rs}_frac"] for rs in ("DSP", "LUT", "FF", "BRAM_18K")
                    )
                    r["feasible"] = r["max_frac"] <= 1.0
                    rows.append(r)
    return pd.DataFrame(rows)


def decisions(df: pd.DataFrame) -> pd.DataFrame:
    out = []
    for (band, target, srs, side), g in df.groupby(["band", "target", "srs", "side"]):
        for lb, tb, cm in itertools.product(LOSS_BUDGETS, TIME_BUDGETS_MS, COSTS):
            col = "max_frac" if cm == "max" else f"{cm}_frac"
            ok = g[
                (g.loss_med <= lb)
                & (g.loss_p90 <= lb)
                & (g.time_p90_ms <= tb)
                & g.feasible
            ]
            rec = {"band": band, "target": target, "srs": srs, "side": side, "loss_budget_db": lb,
                   "time_budget_ms": tb, "cost": cm, "n_meeting": len(ok)}  # fmt: skip
            if len(ok):
                best = ok.loc[ok[col].idxmin()]
                rec.update({c: best[c] for c in ("form", "R", "C", "W", "grouping", "units", "eig",
                                                 "regime", "t", "loss_med", "loss_p90",
                                                 "time_p90_ms", "max_frac")})  # fmt: skip
                rec["cost_value"] = best[col]
            out.append(rec)
    return pd.DataFrame(out)


def frontiers(df: pd.DataFrame) -> pd.DataFrame:
    """Per (band, scenario, array, loss budget): the Pareto set of (time, max fraction)."""
    out = []
    for (band, target, srs, side), g in df.groupby(["band", "target", "srs", "side"]):
        for lb in LOSS_BUDGETS:
            ok = g[(g.loss_med <= lb) & (g.loss_p90 <= lb) & g.feasible].sort_values(
                ["time_p90_ms", "max_frac"]
            )
            best = math.inf
            for _, r in ok.iterrows():
                if r.max_frac < best:
                    best = r.max_frac
                    out.append({**r.to_dict(), "loss_budget_db": lb})
    return pd.DataFrame(out)


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--bands", nargs="+", default=["100MHz", "1.44MHz"])
    ap.add_argument("--srs", nargs="+", default=["10dB", "none"])
    a = ap.parse_args(argv)
    acc = pd.read_csv(PAPER / "accuracy_losses.csv")
    delta = json.loads((sp.PREREG.parent / "reports" / "rtl_fit.json").read_text())[
        "delta"
    ]
    tables = []
    for band, target, srs, side in itertools.product(
        a.bands, ("-6_3", "-6_14"), a.srs, ARRAYS
    ):
        t = build_table(band, target, srs, side, acc, delta)
        print(f"{band} {target} {srs} {side}x{side}: {len(t)} designs", flush=True)
        tables.append(t)
    df = pd.concat(tables, ignore_index=True)
    PAPER.mkdir(parents=True, exist_ok=True)
    df.to_csv(PAPER / "dse_designs.csv.gz", index=False, float_format="%.6g")
    decisions(df).to_csv(PAPER / "dse_decisions.csv", index=False, float_format="%.6g")
    frontiers(df).to_csv(PAPER / "dse_frontiers.csv", index=False, float_format="%.6g")
    print(
        f"wrote {PAPER / 'dse_designs.csv.gz'} ({len(df)} rows); decisions; frontiers"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
