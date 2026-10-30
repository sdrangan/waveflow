"""findings.py -- the DSE's findings, in numbers, each pointing at its table (plan step 6.1, AC12).

``plans/ltbf_dse/ltbf_dse.md`` AC12.  Reads the DSE's tables (``hw/dse.py``) and the accuracy sweep's,
runs the one experiment they do not hold -- the slot beside a long-term update (finding 4's
interference, ``jobsim`` with ``overlap``) -- and writes ``paper_data/FINDINGS.md`` (local):

1. the cheapest design per array meeting each loss and T_LT budget, and the real-time load it
   sustains with ingest (``dse_decisions.csv``, ``dse_realtime.csv``);
2. explicit against factored ``Q̂`` (``dse_designs.csv``);
3. iterations against word length (``accuracy_losses.csv``, ``dse_designs.csv``);
4. where the time goes: engine, Shared Mem port and A53 busy shares of T_LT (``dse_designs.csv``),
   the real-time load against 100 MHz, Shared Mem's what-ifs (a one-word-a-cycle leaf, banks:
   ``dse_whatif.csv``) and the slot's interference with a long-term update (``dse_interference.csv``).

Vitis HLS / Vivado 2024.1 for every built number; Shared Mem as built (a read word two cycles).

    python -m examples.ltbf_dse.hw.findings
"""

from __future__ import annotations

import csv
import dataclasses
import json
from pathlib import Path

from examples.ltbf_dse.hw import dse

RE_TARGET = 14 * 792 / 125e-6  # 100 MHz: 88.7 M RE/s


def _read(name: str) -> list[dict]:
    with (dse.PAPER / name).open() as fh:
        return list(csv.DictReader(fh))


def _f(x) -> float:
    return float(x) if x not in ("", None) else float("nan")


def interference() -> list[dict]:
    """Per array (L 4, factored, t 3, drop 1 sector 1): the slot alone after the update against the
    slot from the start beside it -- the update's time and the slot's end."""
    from examples.ltbf_dse.hw import jobsim as js
    from examples.ltbf_dse.hw import models as hm
    from examples.ltbf_dse.hw import system as sy

    model = json.loads(hm.MODELS.read_text())
    rows = []
    rt = {
        (int(r["n"]), int(r["L"])): _f(r["period_cycles"])
        for r in _read("dse_realtime.csv")
    }
    for n in dse.ARRAYS:
        period = 1.5 * rt[n, 4] / dse.F_HOST  # a pace each runs alone with margin
        cfg, timing = js.fitted(
            sy.LtbfSystem(sy.inputs(dse._params(n, sym_period=period))), model
        )
        alone = js.JobSim(cfg, timing).run()
        both = js.JobSim(dataclasses.replace(cfg, overlap=True), timing).run()
        slot_alone = (alone["end"] - alone["t_lt"]) * dse.F_HOST
        rows.append({"array": f"{n}x{n}", "sym_period_cycles": period * dse.F_HOST,
                     "t_lt_alone": alone["t_lt"] * dse.F_HOST, "t_lt_beside_slot": both["t_lt"] * dse.F_HOST,
                     "slot_alone": slot_alone, "end_together": both["end"] * dse.F_HOST,
                     "overruns_together": both["overruns"], "dropped_together": both["dropped"]})  # fmt: skip
    dse._write("dse_interference.csv", rows)
    return rows


#: Shared Mem what-ifs (user, 2026-10-29): (label, read_scale, banks).
WHAT_IFS = (
    ("as built", 1.0, 1),
    ("one-word-a-cycle leaf", 0.5, 1),
    ("2 banks", 1.0, 2),
    ("both", 0.5, 2),
)


def what_if() -> list[dict]:
    """Per array (L 4, factored, ``t`` 3, drop 1 sector 1) and Shared Mem what-if: one sector's
    long-term update and the slot's sustained RE rate (bisection, as ``dse._rt_case``).
    """
    import math

    from examples.ltbf_dse.hw import jobsim as js
    from examples.ltbf_dse.hw import models as hm
    from examples.ltbf_dse.hw import system as sy

    model = json.loads(hm.MODELS.read_text())
    rows = []
    for n in dse.ARRAYS:
        S = sy.LtbfSystem(sy.inputs(dse._params(n)))
        for label, scale, banks in WHAT_IFS:
            cfg, timing = js.fitted(S, model, read_scale=scale, banks=banks)
            lt = (
                js.JobSim(dataclasses.replace(cfg, lt_only=True), timing).run()["t_lt"]
                * dse.F_HOST
            )

            def ok(c: float, cfg=cfg, timing=timing) -> bool:
                r = js.JobSim(
                    dataclasses.replace(cfg, sym_period=c / dse.F_HOST), timing
                ).run()
                return js.sustained(r, c)

            lo, hi = 1_000.0, 250_000.0 * n * n / 64
            while not ok(hi):
                hi *= 2
            while hi / lo > 1.02:
                mid = math.sqrt(lo * hi)
                lo, hi = (lo, mid) if ok(mid) else (mid, hi)
            rows.append({"array": f"{n}x{n}", "shared_mem": label, "t_lt_sector_cycles": lt,
                         "period_cycles": hi, "re_per_s": 792 / (hi / dse.F_HOST)})  # fmt: skip
    dse._write("dse_whatif.csv", rows)
    return rows


def main() -> Path:
    designs = _read("dse_designs.csv")
    decisions = _read("dse_decisions.csv")
    rt = _read("dse_realtime.csv")
    acc = _read("accuracy_losses.csv")
    inter = interference()
    wif = what_if()
    L = []
    w = L.append
    w("# LTBF DSE -- findings\n")
    w(
        "Every built number: Vitis HLS / Vivado 2024.1, xczu48dr at 250 MHz.  Shared Mem as built (the framework's "
        "BRAM leaf: a read word two cycles, a write one, one transaction at a time).  A design: the systolic array, "
        "`W`, the vector unit's lanes, the form and `t` at `r` 1, `Mmax` 16, `c` 8, `J` 9, 64-bit words, the dynamic "
        "policy, one A53 core.  T_LT: the three sectors' long-term updates back to back, the worst of drops 1, 11, 12.\n"
    )
    # 1 -------------------------------------------------------------------------------------------
    w("## 1. The cheapest design per array (`dse_decisions.csv`, `dse_realtime.csv`)\n")
    w(
        "Cost measure `max` (the largest resource fraction).  '--': no design meets both budgets.\n"
    )
    w(
        "| array | target | loss budget (dB) | T_LT budget (ms) | design | median loss (dB) | T_LT (ms) | max cost | RE/s sustained (of 88.7 M) |"
    )
    w("|---|---|---|---|---|---|---|---|---|")
    for d in decisions:
        if d["cost_measure"] != "max":
            continue
        if d.get("R"):
            design = f"{d['R']}x{d['C']} W{d['W']} L{d['L']} {d['form']} t{d['t']}"
            mo = " (model-only: DDR)" if d.get("model_only") == "True" else ""
            w(
                f"| {d['array']} | {d['target']} | {d['loss_budget_db']} | {d['time_budget_ms']} | {design}{mo} | "
                f"{_f(d['median_loss_db']):.3f} | {_f(d['t_lt_ms']):.2f} | {_f(d['cost_max']):.1%} | "
                f"{_f(d['re_per_s']) / 1e6:.2f} M ({_f(d['re_per_s']) / RE_TARGET:.2%}) |"
            )
        else:
            w(
                f"| {d['array']} | {d['target']} | {d['loss_budget_db']} | {d['time_budget_ms']} | -- | | | | |"
            )
    w("")
    for r in rt:
        w(
            f"* {r['n']}x{r['n']}, {r['L']} lanes: a symbol every {_f(r['period_cycles']):,.0f} cycles at best, "
            f"{_f(r['re_per_s']) / 1e6:.2f} M RE/s = {_f(r['re_per_s']) / RE_TARGET:.2%} of 100 MHz with ingest."
        )
    w("")
    # 2 -------------------------------------------------------------------------------------------
    w("## 2. Explicit against factored Q̂ (`dse_designs.csv`)\n")
    w(
        "| array | t | T_LT factored (ms) | T_LT explicit (ms) | Shared Mem words factored | explicit | median loss f / e (dB, -6_14, W16) |"
    )
    w("|---|---|---|---|---|---|---|")
    by = {
        (r["array"], r["form"], r["t"], r["L"], r["W"], r["R"], r["C"], r["target"]): r
        for r in designs
    }
    for n in dse.ARRAYS:
        a = f"{n}x{n}"
        for t in dse.TS:
            f = by.get((a, "factored", str(t), "4", "16", "4", "8", "-6_14"))
            e = by.get((a, "explicit", str(t), "4", "16", "4", "8", "-6_14"))
            if f and e:
                w(
                    f"| {a} | {t} | {_f(f['t_lt_ms']):.2f} | {_f(e['t_lt_ms']):.2f} | {int(_f(f['mem_words'])):,} | "
                    f"{int(_f(e['mem_words'])):,} | {_f(f['median_loss_db']):.3f} / {_f(e['median_loss_db']):.3f} |"
                )
    w("")
    # 3 -------------------------------------------------------------------------------------------
    w(
        "## 3. Iterations against word length (`accuracy_losses.csv`, `dse_designs.csv`)\n"
    )
    w("Median loss (dB), factored, `r` 1; T_LT (ms) at 4 lanes.\n")
    w("| array | target | W | " + " | ".join(f"t {t}" for t in dse.TS) + " |")
    w("|---|---|---|" + "---|" * len(dse.TS))
    loss = {
        (r["array"], r["target"], r["config"], r["t"], r["r"]): _f(r["median_loss_db"])
        for r in acc
    }
    for n in dse.ARRAYS:
        for tg in dse.TARGETS:
            for W in dse.WIDTHS:
                vals = [
                    loss.get((f"{n}x{n}", tg, f"W{W}g1l16f", str(t), "1"))
                    for t in dse.TS
                ]
                w(
                    f"| {n}x{n} | {tg} | {W} | "
                    + " | ".join("" if v is None else f"{v:.3f}" for v in vals)
                    + " |"
                )
        tl = [
            by.get((f"{n}x{n}", "factored", str(t), "4", "16", "4", "8", "-6_14"))
            for t in dse.TS
        ]
        w(
            f"| {n}x{n} | T_LT (ms) | | "
            + " | ".join("" if r is None else f"{_f(r['t_lt_ms']):.2f}" for r in tl)
            + " |"
        )
    w("")
    # 4 -------------------------------------------------------------------------------------------
    w("## 4. Where the time goes (`dse_designs.csv`, `dse_interference.csv`)\n")
    w(
        "Busy shares of T_LT (factored, `t` 3, 4 lanes; the engines' jobs end to end, the port with at least one "
        "transfer, the A53's charged cycles):\n"
    )
    w("| array | T_LT (ms) | systolic | vector unit | Shared Mem port | A53 |")
    w("|---|---|---|---|---|---|")
    for n in dse.ARRAYS:
        r = by.get((f"{n}x{n}", "factored", "3", "4", "16", "4", "8", "-6_14"))
        if r:
            w(
                f"| {n}x{n} | {_f(r['t_lt_ms']):.2f} | {_f(r['sys_busy']):.0%} | {_f(r['vec_busy']):.0%} | "
                f"{_f(r['port_busy']):.0%} | {_f(r['cpu_busy']):.0%} |"
            )
    w("")
    w(
        "The systolic engine is bound by Shared Mem's port: at RTL a job takes about 2.13 cycles a word it reads "
        "(the fitted model, held-out 0.2%), the same for a 2 x 4 and an 8 x 8 array (368,240-368,345 cycles a gate at "
        "N_rx 256), so the array's size buys nothing as built; a one-word-a-cycle leaf would halve it (a what-if).\n"
    )
    w(
        "Shared Mem what-ifs (`dse_whatif.csv`; L 4, factored, `t` 3, drop 1 sector 1): one sector's long-term "
        "update and the slot's sustained rate with ingest.\n"
    )
    w("| array | Shared Mem | one sector's update (ms) | RE/s sustained | of 100 MHz |")
    w("|---|---|---|---|---|")
    for r in wif:
        w(
            f"| {r['array']} | {r['shared_mem']} | {_f(r['t_lt_sector_cycles']) / dse.F_HOST * 1e3:.2f} | "
            f"{_f(r['re_per_s']) / 1e6:.2f} M | {_f(r['re_per_s']) / RE_TARGET:.2%} |"
        )
    w("")
    w(
        "The slot beside a long-term update (`dse_interference.csv`; L 4, factored, `t` 3, drop 1 sector 1, the "
        "O-RU at 1.5x the shortest period the slot sustains alone, the slot and the update from the same start):\n"
    )
    w(
        "| array | symbol period (cycles) | update alone (cycles) | update beside the slot | symbols dropped (of 14) |"
    )
    w("|---|---|---|---|---|")
    for r in inter:
        a, b = _f(r["t_lt_alone"]), _f(r["t_lt_beside_slot"])
        w(
            f"| {r['array']} | {_f(r['sym_period_cycles']):,.0f} | {a:,.0f} | {b:,.0f} ({b / a - 1:+.1%}) | "
            f"{r['overruns_together']} |"
        )
    w("")
    out = dse.PAPER / "FINDINGS.md"
    out.write_text("\n".join(L) + "\n", encoding="utf-8")
    return out


if __name__ == "__main__":
    print(main())
