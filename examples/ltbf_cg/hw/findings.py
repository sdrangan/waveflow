"""findings.py -- the five findings of §9 (d), in numbers, from the DSE's tables.

``plans/mimo_cg/ltbf_cg_dse.md`` step 5.2 (AC8).  Reads ``paper_data/dse_designs.csv.gz``,
``dse_decisions.csv``, ``accuracy_losses.csv`` and ``fidelity_*.csv``; writes
``paper_data/FINDINGS.md`` (local, like every result of this study) and its tables:

1. the cheapest design meeting each loss and time budget, per array (``findings_cheapest.csv``);
2. the explicit-vs-factored crossover: per array, the fastest feasible design of each form at a loss
   budget, and the smallest ``t`` at which explicit is faster (``findings_form.csv``);
3. iterations vs word length: per array and loss budget, the narrowest width meeting it at each
   ``t`` (``findings_width.csv``);
4. where the eigendecomposition step should run: the update time with the A53 doing all of it vs
   the engine forming ``M_i`` and ``G_i`` (``findings_eig.csv``);
5. decision fidelity (the pre-registered AC9 score).

    python -m examples.ltbf_cg.hw.findings
"""

from __future__ import annotations

import sys

import numpy as np
import pandas as pd  # type: ignore[import-untyped]

from examples.ltbf_cg.hw import dse

PAPER = dse.PAPER
MAIN = {"band": "100MHz", "srs": "10dB"}


def _md_table(df: pd.DataFrame, cols: list[str]) -> str:
    head = "| " + " | ".join(cols) + " |\n|" + "---|" * len(cols) + "\n"
    body = "".join(
        "| "
        + " | ".join(
            f"{r[c]:.3g}" if isinstance(r[c], float) else str(r[c]) for c in cols
        )
        + " |\n"
        for _, r in df.iterrows()
    )
    return head + body


def cheapest(dec: pd.DataFrame) -> pd.DataFrame:
    d = dec[(dec.cost == "max")].copy()
    d["design"] = d.apply(
        lambda r: "none" if pd.isna(r.get("form")) else
        f"{r.form} {int(r.R)}x{int(r.C)} W{int(r.W)} {r.grouping} u{int(r.units)} {r.eig} reg{int(r.regime)} t{int(r.t)}",
        axis=1,
    )  # fmt: skip
    return d


def form_crossover(df: pd.DataFrame, lb: float = 0.5) -> pd.DataFrame:
    out = []
    for (band, target, srs, side), g in df.groupby(["band", "target", "srs", "side"]):
        ok = g[
            (g.loss_med <= lb)
            & (g.loss_p90 <= lb)
            & g.feasible
            & (g.units == 1)
            & (g.eig == "pl")
        ]
        rec = {
            "band": band,
            "target": target,
            "srs": srs,
            "side": side,
            "loss_budget_db": lb,
        }
        for form in ("factored", "explicit"):
            f = ok[ok.form == form]
            rec[f"{form}_fastest_ms"] = float(f.time_p90_ms.min()) if len(f) else np.nan
        # at a fixed engine (4 x 4, W16, joint, regime 1) the smallest t where explicit is faster
        same = g[(g.R == 4) & (g.C == 4) & (g.W == 16) & (g.grouping == "joint") & (g.regime == 1)
                 & (g.units == 1) & (g.eig == "pl")]  # fmt: skip
        piv = same.pivot_table(index="t", columns="form", values="time_p90_ms")
        cross = [
            int(t)
            for t, r in piv.iterrows()
            if r.get("explicit", np.inf) < r.get("factored", np.inf)
        ]
        rec["explicit_faster_from_t"] = min(cross) if cross else None
        out.append(rec)
    return pd.DataFrame(out)


def width_vs_t(acc: pd.DataFrame) -> pd.DataFrame:
    a = acc[
        (acc.kind == "fixed")
        & (acc.r == 1)
        & (acc.nue == 10)
        & (acc.form == "factored")
    ].copy()
    a["regime"], a["t"] = a.regime.astype(int), a.t.astype(int)
    a = a[a.regime == 1]
    a["W"] = a.format.str.extract(r"W(\d+)g1").astype(float)
    a = a.dropna(subset=["W"])
    out = []
    for (band, target, srs, side), g in a.groupby(["band", "target", "srs", "n"]):
        for lb in dse.LOSS_BUDGETS:
            rec = {
                "band": band,
                "target": target,
                "srs": srs,
                "side": side,
                "loss_budget_db": lb,
            }
            for t in range(1, 11):
                ok = g[(g.t == t) & (g.median_loss_db <= lb) & (g.p90_loss_db <= lb)]
                rec[f"t{t}"] = int(ok.W.min()) if len(ok) else None
            out.append(rec)
    return pd.DataFrame(out)


def eig_place(df: pd.DataFrame, lb: float = 0.5) -> pd.DataFrame:
    out = []
    for (band, target, srs, side), g in df.groupby(["band", "target", "srs", "side"]):
        ok = g[(g.loss_med <= lb) & (g.loss_p90 <= lb) & g.feasible]
        rec = {
            "band": band,
            "target": target,
            "srs": srs,
            "side": side,
            "loss_budget_db": lb,
        }
        for eig in dse.EIG:
            e = ok[ok.eig == eig]
            rec[f"{eig}_fastest_ms"] = float(e.time_p90_ms.min()) if len(e) else np.nan
        out.append(rec)
    return pd.DataFrame(out)


def main() -> int:
    df = pd.read_csv(PAPER / "dse_designs.csv.gz")
    dec = pd.read_csv(PAPER / "dse_decisions.csv")
    acc = pd.read_csv(PAPER / "accuracy_losses.csv")
    ch = cheapest(dec)
    ch.to_csv(PAPER / "findings_cheapest.csv", index=False)
    fo = form_crossover(df)
    fo.to_csv(PAPER / "findings_form.csv", index=False)
    wt = width_vs_t(acc)
    wt.to_csv(PAPER / "findings_width.csv", index=False)
    ep = eig_place(df)
    ep.to_csv(PAPER / "findings_eig.csv", index=False)
    fid = {}
    for eig in ("a53", "pl"):
        p = PAPER / f"fidelity_{eig}.csv"
        if p.is_file():
            fid[eig] = pd.read_csv(p).groupby("cost")["right"].mean().round(3).to_dict()
    main_ch = ch[(ch.band == MAIN["band"]) & (ch.srs == MAIN["srs"])]
    text = [
        "# LTBF CG design-space exploration: findings\n",
        "Generated by `python -m examples.ltbf_cg.hw.findings` from the local tables in this directory "
        "(plans/mimo_cg/ltbf_cg_dse.md §9 d).  Loss: exact LTBF minus the design, dB, signed, per UE; "
        "a budget holds when the median and the 90th percentile are within it.  Time: one long-term "
        "update of three sectors, 90th percentile over 100 drops.  Cost: the largest device fraction.\n",
        "## 1. The cheapest design per loss and time budget (100 MHz, nominal SRS; `findings_cheapest.csv`)\n",
        _md_table(main_ch, ["target", "side", "loss_budget_db", "time_budget_ms", "n_meeting", "design", "cost_value", "time_p90_ms"]),
        "\n## 2. Explicit vs factored (loss budget 0.5 dB, one engine; `findings_form.csv`)\n",
        _md_table(fo[(fo.band == MAIN["band"])], ["target", "srs", "side", "factored_fastest_ms", "explicit_fastest_ms", "explicit_faster_from_t"]),
        "\n## 3. Iterations vs word length (regime 1, factored; narrowest W meeting the budget at each t; `findings_width.csv`)\n",
        _md_table(wt[(wt.band == MAIN["band"])], ["target", "srs", "side", "loss_budget_db"] + [f"t{t}" for t in range(1, 11)]),
        "\n## 4. Where the eigendecomposition step runs (loss budget 0.5 dB; `findings_eig.csv`)\n",
        _md_table(ep[(ep.band == MAIN["band"])], ["target", "srs", "side", "a53_fastest_ms", "pl_fastest_ms"]),
        "\n## 5. Decision fidelity (AC9, pre-registered; fraction right per cost measure)\n",
        "\n".join(f"- `{k}`: {v}" for k, v in fid.items()) or "- not scored yet",
        "\n",
    ]  # fmt: skip
    (PAPER / "FINDINGS.md").write_text("\n".join(text))
    print(f"wrote {PAPER / 'FINDINGS.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
