"""findings.py -- the five findings of §9 (d), in numbers and sentences, from the DSE's tables.

``plans/mimo_cg/ltbf_cg_dse.md`` step 5.2 (AC8).  Reads ``paper_data/dse_designs.csv.gz``,
``dse_decisions.csv``, ``accuracy_losses.csv`` and ``fidelity_*.csv``; writes
``paper_data/FINDINGS.md`` (local, like every result of this study) and its tables:

1. the cheapest design meeting each loss and time budget, per array -- over the **built** options
   (the calibrated A53 doing M_i, Jacobi and G_i; one value per I/O beat) and, as a what-if, over
   **all** options including the modeled ones (``findings_cheapest.csv``); with the absolute SINR
   and the reference's own gap to MMSE (``findings_reference.csv``);
2. the explicit-vs-factored crossover per array and placement, and against UEs per sector at fixed
   ``t`` (``findings_form.csv``, ``findings_nue.csv``);
3. iterations vs word length (``findings_width.csv``);
4. where the eigendecomposition step should run (``findings_eig.csv``);
5. decision fidelity (AC9), split into decisions where some design is feasible and the rest.

    python -m examples.ltbf_cg.hw.findings
"""

from __future__ import annotations

import json
import sys

import numpy as np
import pandas as pd  # type: ignore[import-untyped]

from examples.ltbf_cg.hw import dse

PAPER = dse.PAPER
MAIN = {"band": "100MHz", "srs": "10dB"}
MODELED_EIG = {"a53x4", "pl"}


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


def _design(r) -> str:
    if pd.isna(r.get("form")):
        return "none"
    modeled = (
        r.eig in MODELED_EIG or int(r.L) > 1 or (r.form == "explicit" and r.side == 32)
    )
    return (
        f"{r.form} {int(r.R)}x{int(r.C)} W{int(r.W)} {r.grouping} u{int(r.units)} {r.eig} "
        f"L{int(r.L)} t{int(r.t)}" + (" [modeled]" if modeled else "")
    )


def cheapest(dec: pd.DataFrame) -> pd.DataFrame:
    d = dec[dec.cost == "max"].copy()
    d["design"] = d.apply(_design, axis=1)
    return d


def reference(acc: pd.DataFrame) -> pd.DataFrame:
    """Exact LTBF against MMSE, and the float CG at t = 1 (a scaled matched filter on the SRS)."""
    out = []
    for (band, target, srs, side, nue), g in acc.groupby(
        ["band", "target", "srs", "n", "nue"]
    ):
        if nue != 10:
            continue
        ex = g[(g.kind == "exact") & (g.r.astype(str) == "1")]
        t1 = g[(g.kind == "float") & (g.r.astype(str) == "1") & (g.regime.astype(str) == "1")
               & (g.t.astype(str) == "1")]  # fmt: skip
        if ex.empty:
            continue
        out.append({
            "band": band, "target": target, "srs": srs, "side": side,
            "mmse_minus_exact_db": float(ex.median_loss_db.iloc[0]),
            "exact_sinr_db": float(ex.median_sinr_db.iloc[0]),
            "t1_sinr_db": float(t1.median_sinr_db.iloc[0]) if len(t1) else np.nan,
        })  # fmt: skip
    return pd.DataFrame(out)


def form_crossover(df: pd.DataFrame, lb: float = 0.5) -> pd.DataFrame:
    """Per array and placement (L = 1, one engine): the fastest feasible design of each form within
    the loss budget, and at a fixed engine (4 x 4, W16, joint, regime 1) the smallest t from which
    explicit is faster.  Explicit at 32 x 32 keeps Q in DDR: modeled."""
    out = []
    for (band, target, srs, side), g in df.groupby(["band", "target", "srs", "side"]):
        for eig in ("a53", "pl"):
            h = g[(g.L == 1) & (g.units == 1) & (g.eig == eig)]
            ok = h[(h.loss_med <= lb) & (h.loss_p90 <= lb) & h.feasible]
            rec = {"band": band, "target": target, "srs": srs, "side": side, "eig": eig,
                   "loss_budget_db": lb}  # fmt: skip
            for form in ("factored", "explicit"):
                f = ok[ok.form == form]
                rec[f"{form}_fastest_ms"] = (
                    float(f.time_p90_ms.min()) if len(f) else np.nan
                )
            same = h[(h.R == 4) & (h.C == 4) & (h.W == 16) & (h.grouping == "joint")
                     & (h.regime == 1)]  # fmt: skip
            piv = same.pivot_table(index="t", columns="form", values="time_p90_ms")
            cross = [
                int(t) for t, r in piv.iterrows()
                if r.get("explicit", np.inf) < r.get("factored", np.inf)
            ]  # fmt: skip
            rec["explicit_faster_from_t"] = min(cross) if cross else None
            out.append(rec)
    return pd.DataFrame(out)


def width_vs_t(acc: pd.DataFrame) -> pd.DataFrame:
    """The narrowest width at guard 1 meeting the budget at each t (regime 1, factored); the grid's
    narrowest is 10, so 10 means <= 10."""
    a = acc[(acc.kind == "fixed") & (acc.r.astype(str) == "1") & (acc.nue == 10)
            & (acc.form == "factored")].copy()  # fmt: skip
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
        ok = g[(g.loss_med <= lb) & (g.loss_p90 <= lb) & g.feasible & (g.L == 1)]
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


def nue_crossover(acc: pd.DataFrame, ts=(3, 5, 10)) -> pd.DataFrame:
    """16 x 16, 100 MHz, [-6, 3] dB, nominal SRS, UEs per sector 4..40, at a fixed engine (4 x 4,
    W16, joint, one unit, M_i and G_i on the engine) and fixed t: the p90 update time per form and
    regime, and the median loss.  Regime 2's X_sym storage and I/O are not costed: its times are
    lower bounds."""
    from examples.ltbf_cg.hw import models as md
    from examples.ltbf_cg.hw import space as sp
    from examples.ltbf_cg.hw import timing as tm

    delta = json.loads((sp.PREREG.parent / "reports" / "rtl_fit.json").read_text())[
        "delta"
    ]
    params = md.load()
    counters = tm.a53_counters("10dB")
    a = acc[(acc.kind == "fixed") & (acc.r.astype(str) == "1") & (acc.band == "100MHz")
            & (acc.target == "-6_3") & (acc.srs == "10dB") & (acc.n == 16)
            & (acc.format == "W16g1")].copy()  # fmt: skip
    a["regime"], a["t"] = a.regime.astype(int), a.t.astype(int)
    out = []
    for nue in (4, 10, 20, 40):
        drops = (
            tm.sector_counts_nue(nue)
            if nue != 10
            else tm.sector_counts("100MHz", "-6_3", 256)
        )
        cmax = 8 * max(nc for d in drops for nc, _ in d)
        cmax = next(c for c in (96, 240, 480, 960) if c >= cmax)
        for form, regime, t in (
            (f, r, t) for f in ("factored", "explicit") for r in (1, 2) for t in ts
        ):
            k = sp.Knobs(form == "explicit", 256, cmax, 80, 4, 4, 16)
            o = md.predict_overheads(params, k)
            g = a[(a.nue == nue) & (a.form == form) & (a.regime == regime) & (a.t == t)]
            vals = [
                tm.update_time_s(
                    k, o, tm.Run(t, regime, 1, "pl"), d, delta, 1, counters
                )
                for d in drops
            ]
            out.append({
                "nue": nue, "cmax": cmax, "form": form, "regime": regime, "t": t,
                "loss_med": float(g.median_loss_db.iloc[0]) if len(g) else np.nan,
                "time_p90_ms": float(np.percentile(vals, 90) * 1e3),
            })  # fmt: skip
    return pd.DataFrame(out)


def fidelity_split() -> dict:
    """AC9 per cost measure: over all registered decisions, and over the ones where at least one
    side (predicted or measured) finds a feasible design."""
    out = {}
    for eig in ("a53", "pl"):
        p = PAPER / f"fidelity_{eig}.csv"
        if not p.is_file():
            continue
        sc = pd.read_csv(p)
        nt = sc[(sc.n_pred_ok > 0) | (sc.n_meas_ok > 0)]
        cnt = nt.groupby("cost")["right"].agg(["sum", "count"])
        out[eig] = {
            "all": sc.groupby("cost")["right"].mean().round(3).to_dict(),
            "nontrivial": {
                c: f"{int(r['sum'])}/{int(r['count'])}" for c, r in cnt.iterrows()
            },
            "sides_with_feasible": sorted(int(s) for s in nt.side.unique()),
        }
    return out


def _sentences(ch, ref, fo, wt, ep, fid) -> list[str]:
    s = []
    b = ch[(ch.options == "built") & (ch.band == MAIN["band"]) & (ch.srs == MAIN["srs"])
           & (ch.loss_budget_db == 0.5) & (ch.time_budget_ms == 10)]  # fmt: skip
    for _, r in b.sort_values(["target", "side"]).iterrows():
        tail = (
            ""
            if pd.isna(r.get("form"))
            else f", {r.max_frac:.3f} of the device, p90 time {r.time_p90_ms:.2f} ms, "
            f"median SINR {r.sinr_med:.1f} dB"
        )
        s.append(
            f"- **(1)** {r.side}x{r.side}, [{r.target.replace('_', ', ')}] dB, 0.5 dB, 10 ms, built "
            f"options: {_design(r)}{tail}"
        )
    rr = ref[
        (ref.band == MAIN["band"]) & (ref.srs == MAIN["srs"]) & (ref.target == "-6_3")
    ]
    if len(rr):
        gaps = " / ".join(
            f"{x:.1f}" for x in rr.sort_values("side").mmse_minus_exact_db
        )
        s.append(
            "- **(1, the reference)** With nominal SRS the CG at t = 1 (X1 = alpha B, a per-column "
            "scaled matched filter on the SRS; Q enters only through alpha) beats exact LTBF, "
            f"which is itself {gaps} dB below MMSE at 8x8 / 16x16 / 32x32 ([-6, 3] dB): early "
            "stopping regularizes a covariance estimate the per-antenna SRS SNR cannot support, so "
            "there the loss-budget axis is degenerate (t = 1 with W10 meets every budget)."
        )
    f = fo[
        (fo.band == MAIN["band"])
        & (fo.srs == "none")
        & (fo.target == "-6_3")
        & (fo.eig == "a53")
    ]
    for _, r in f.sort_values("side").iterrows():
        tail = (
            f"; at a fixed 4x4 W16 engine explicit is faster from t = {int(r.explicit_faster_from_t)}"
            if pd.notna(r.explicit_faster_from_t)
            else "; explicit is never faster"
        )
        s.append(
            f"- **(2)** {r.side}x{r.side} (noiseless SRS, [-6, 3] dB, A53 placement, one engine): "
            f"fastest factored {r.factored_fastest_ms:.2f} ms vs explicit "
            f"{r.explicit_fastest_ms:.2f} ms{tail}"
            + (" (explicit here keeps Q in DDR: modeled)" if r.side == 32 else "")
        )
    w = wt[(wt.band == MAIN["band"]) & (wt.srs == "none") & (wt.target == "-6_14")
           & (wt.loss_budget_db == 0.1)]  # fmt: skip
    for _, r in w.sort_values("side").iterrows():
        cells = ", ".join(
            f"t{t}: W{int(r[f't{t}'])}" for t in range(1, 11) if pd.notna(r[f"t{t}"])
        )
        s.append(
            f"- **(3)** {r.side}x{r.side} (noiseless, [-6, 14] dB, 0.1 dB): narrowest width per t "
            f"-- {cells or 'none'}"
        )
    e = ep[(ep.band == MAIN["band"]) & (ep.srs == MAIN["srs"]) & (ep.target == "-6_3")]
    for _, r in e.sort_values("side").iterrows():
        s.append(
            f"- **(4)** {r.side}x{r.side} (nominal, [-6, 3] dB, 0.5 dB): fastest update with the "
            f"scalar A53 doing M_i, Jacobi, G_i {r.a53_fastest_ms:.2f} ms; a 4x A53 kernel "
            f"{r.a53x4_fastest_ms:.2f} ms (modeled); M_i, G_i on the engine "
            f"{r.pl_fastest_ms:.2f} ms (modeled)"
        )
    a = fid.get("a53")
    if a:
        s.append(
            f"- **(5)** AC9 (registered, `a53`): {a['all']} of all 270 decisions; on the decisions "
            f"where some design is feasible (arrays {a['sides_with_feasible']}): {a['nontrivial']}. "
            "32x32 is untested (no design is feasible there with the scalar A53)."
        )
    return s


def main() -> int:
    df = pd.read_csv(PAPER / "dse_designs.csv.gz")
    dec = pd.read_csv(PAPER / "dse_decisions.csv")
    acc = pd.read_csv(PAPER / "accuracy_losses.csv")
    ch = cheapest(dec)
    ch.to_csv(PAPER / "findings_cheapest.csv", index=False)
    ref = reference(acc)
    ref.to_csv(PAPER / "findings_reference.csv", index=False)
    fo = form_crossover(df)
    fo.to_csv(PAPER / "findings_form.csv", index=False)
    wt = width_vs_t(acc)
    wt.to_csv(PAPER / "findings_width.csv", index=False)
    ep = eig_place(df)
    ep.to_csv(PAPER / "findings_eig.csv", index=False)
    nu = nue_crossover(acc)
    nu.to_csv(PAPER / "findings_nue.csv", index=False)
    fid = fidelity_split()
    m = (ch.band == MAIN["band"]) & (ch.srs == MAIN["srs"])
    cols1 = ["target", "side", "loss_budget_db", "time_budget_ms", "n_meeting", "design",
             "cost_value", "time_p90_ms", "loss_med", "loss_p90", "sinr_med"]  # fmt: skip
    fid_lines = "\n".join(
        f"- `{k}`: all decisions {v['all']}; decisions with a feasible design {v['nontrivial']} "
        f"(arrays {v['sides_with_feasible']})"
        for k, v in fid.items()
    )
    text = [
        "# LTBF CG design-space exploration: findings\n",
        (
            "Generated by `python -m examples.ltbf_cg.hw.findings` from the local tables in this "
            "directory (plans/mimo_cg/ltbf_cg_dse.md §9 d).  Loss: exact LTBF minus the design, dB, "
            "signed, per UE, over the 90 drops held out from the formats' profile (11-100); a "
            "budget holds when the median and the 90th percentile are within it.  Time: one "
            "long-term update of three sectors, 90th percentile over the 100 drops.  Cost: the "
            "largest device fraction.  `[modeled]` marks a design that relies on something not "
            "built: the 4x A53 kernel (`a53x4`), M_i and G_i on the engine (`pl`), four values per "
            "I/O beat (L4), or explicit Q in DDR at 32x32; the cost model prices these at no "
            "resources.  Ties go to the first row (regime 1, `a53`, L1, the smallest t).\n"
        ),
        "## The findings in sentences\n",
        "\n".join(_sentences(ch, ref, fo, wt, ep, fid)),
        "\n## 1. The cheapest design per loss and time budget, built options (100 MHz, nominal SRS)\n",
        _md_table(ch[m & (ch.options == "built")], cols1),
        "\n## 1b. The same, all options including the modeled ones (what-if)\n",
        _md_table(ch[m & (ch.options == "all")], cols1),
        "\n## 1c. The reference: exact LTBF against MMSE, the CG at t = 1 (`findings_reference.csv`)\n",
        _md_table(
            ref[ref.band == MAIN["band"]],
            [
                "target",
                "srs",
                "side",
                "mmse_minus_exact_db",
                "exact_sinr_db",
                "t1_sinr_db",
            ],
        ),
        "\n## 2. Explicit vs factored (0.5 dB, L1, one engine; `findings_form.csv`)\n",
        _md_table(
            fo[fo.band == MAIN["band"]],
            [
                "target",
                "srs",
                "side",
                "eig",
                "factored_fastest_ms",
                "explicit_fastest_ms",
                "explicit_faster_from_t",
            ],
        ),
        (
            "\n## 2b. Form and regime against UEs per sector (16x16, nominal, [-6, 3] dB, fixed "
            "engine and t; regime 2 times are lower bounds; `findings_nue.csv`)\n"
        ),
        _md_table(
            nu, ["nue", "cmax", "form", "regime", "t", "loss_med", "time_p90_ms"]
        ),
        (
            "\n## 3. Iterations vs word length (regime 1, factored; narrowest W at guard 1 meeting "
            "the budget per t, 10 meaning <= 10; `findings_width.csv`)\n"
        ),
        _md_table(
            wt[wt.band == MAIN["band"]],
            ["target", "srs", "side", "loss_budget_db"]
            + [f"t{t}" for t in range(1, 11)],
        ),
        "\n## 4. Where the eigendecomposition step runs (0.5 dB, L1; `findings_eig.csv`)\n",
        _md_table(
            ep[ep.band == MAIN["band"]],
            [
                "target",
                "srs",
                "side",
                "a53_fastest_ms",
                "a53x4_fastest_ms",
                "pl_fastest_ms",
            ],
        ),
        "\n## 5. Decision fidelity (AC9, pre-registered) ⚠️\n",
        fid_lines or "- not scored",
        (
            "\nThe 8x8 finalist `e_n64_c240_m8_r4x4_w10` is one of the wrong LUT picks: W12 "
            "measured fewer LUTs (Vitis packs W10 multiplies two per DSP and the packing logic "
            "lands in LUTs); `e_n64_c240_m8_r4x4_w12` was implemented beside it.\n"
        ),
    ]
    fin = PAPER / "finalists_impl.csv"
    if fin.is_file():
        fi = pd.read_csv(fin)
        text += [
            "\n## 6. Finalists after Vivado implementation (AC10; `finalists_impl.csv`)\n",
            _md_table(
                fi,
                [
                    "build",
                    "cp_post_impl_ns",
                    "timing_met",
                    "DSP_impl",
                    "DSP_csynth",
                    "DSP_model",
                    "LUT_impl",
                    "LUT_csynth",
                    "LUT_model",
                    "BRAM_18K_impl",
                    "BRAM_18K_model",
                ],
            ),
            (
                "\nPost-implementation LUT and FF fall well below csynth's estimates, which the "
                "models are fitted to; at 8x8 the W10 design uses fewer LUTs than W12 after "
                "implementation, reversing the csynth ordering behind AC9's LUT shortfall.  The "
                "32x32 design misses 4 ns by 0.19 ns.\n"
            ),
        ]
    (PAPER / "FINDINGS.md").write_text("\n".join(text))
    print(f"wrote {PAPER / 'FINDINGS.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
