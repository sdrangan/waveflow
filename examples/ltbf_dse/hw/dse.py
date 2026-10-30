"""dse.py -- the design-space exploration: accuracy x cost x time of every LTBF design (plan step 6.1).

``plans/ltbf_dse/ltbf_dse.md`` step 6.1, AC12.  A design, per array (8x8, 16x16, 32x32): the
systolic array ``R x C``, the operand width ``W`` (one format triple), the vector unit's lanes ``L``,
the form and the CG iterations ``t`` -- at ``r`` 1, ``Mmax`` 16, ``c`` 8, ``J`` 9, 64-bit words, the
dynamic policy and one A53 core.  For each design and target:

* **loss** -- the accuracy sweep's per-UE loss (``paper_data/accuracy_losses.csv``) at its ``W`` (one
  guard bit, 16-bit lanes), form and ``t``: the median, the 10th percentile (the best UEs) and the
  90th (the worst);
* **cost** -- the frozen resource models' prediction (``hw/models.py``) of its three parts, the Resp
  hub (its csynth), and Shared Mem's storage as built: the framework's memory is block RAM
  (``bram_t2p.v``, ``ram_style = "block"``) at the system's power-of-two span, 512 64-bit words a
  BRAM36 (plan §14, final review).  Each resource a fraction of the xczu48dr; ``max`` the largest.  A
  design over the device is *model-only*: flagged, timed as if on chip (the DDR of plan §9 d is not
  modelled) and never picked.  ``uram_alt``: Shared Mem in URAM instead (4,096 words each), a what-if;
* **T_LT** -- the long-term update of the three sectors back to back on the job-level simulator
  (``jobsim.fitted``: the frozen per-job models, Shared Mem's port shared), the largest over drop 1
  and the subset drops 11-12.  With Shared Mem as built a systolic job's time is the words it moves
  (16-bit lanes at every ``W``), not its array or width, so T_LT is simulated per (array, ``L``, form,
  ``t``); the engine, port and A53 busy cycles go with it (finding 4);
* **real time** -- one sector's sustained RE rate with ingest, the long-term update idle: the
  shortest symbol period the simulator keeps over :data:`RT_SLOTS` slots back to back
  (``jobsim.sustained``: no ring overrun, the O-RU on pace; bisection, drop 1 sector 1), as REs a
  second against 100 MHz's 88.7 M RE/s.

Writes ``paper_data/dse_designs.csv``, ``dse_time.csv``, ``dse_realtime.csv``, ``dse_decisions.csv``
(AC13's rule: the cheapest design per array, target, loss budget, T_LT budget and cost measure) and
``dse_frontiers.csv`` (per array, target, loss budget and T_LT budget, the designs within both that
fit no other beats on max cost, loss and T_LT; budget ``none``: every design, model-only flagged).

    python -m examples.ltbf_dse.hw.dse [--reuse-time]
"""

from __future__ import annotations

import csv
import dataclasses
import itertools
import json
import math
import multiprocessing as mp
import os
import sys
from functools import cache
from pathlib import Path

HERE = Path(__file__).resolve().parent
PAPER = HERE.parent / "paper_data"
#: xczu48dr-ffvg1517-2-e (the csynth reports' AvailableResources).
AVAILABLE = {"LUT": 425280, "FF": 850560, "DSP": 4272, "BRAM_18K": 2160, "URAM": 80}
COSTS = ("DSP", "LUT", "FF", "BRAM_18K", "URAM", "max")
#: 64-bit words a BRAM36 holds (1K x 36 halves, two a word) and a URAM (4K x 72).
BRAM36_WORDS, URAM_WORDS = 512, 4096
#: Slots the sustained-rate check runs back to back.
RT_SLOTS = 3
ARRAYS = (8, 16, 32)
TARGETS = ("-6_14", "-6_3")
#: Systolic array shapes (R, C); ARRAYS are the antenna arrays (n x n).
SYSTOLIC_SHAPES = ((2, 4), (4, 4), (4, 8), (8, 8))
WIDTHS = (12, 14, 16)
LANES = (2, 4)
FORMS = ("factored", "explicit")
TS = (1, 2, 3, 5, 7, 10)
DROPS = (1, 11, 12)
SECTORS = (0, 1, 2)
LOSS_BUDGETS = (0.1, 0.25, 0.5)
TIME_BUDGETS_MS = (1.0, 5.0, 10.0)
F_HOST = 250e6


def _params(n: int, **kw):
    from examples.ltbf_dse.hw import system as sy

    return sy.SysParams(n=n, **kw)


# -- time: the long-term update and the slot's sustained rate -------------------------------------


def _lt_case(args: tuple) -> dict:
    """One (array, drop, sector, form, t): the long-term update's cycles and busy cycles at each
    ``L`` (a sector with no own UE takes none)."""
    n, drop, sector, form, t = args
    from examples.ltbf_dse.hw import jobsim as js
    from examples.ltbf_dse.hw import models as hm
    from examples.ltbf_dse.hw import system as sy

    model = json.loads(hm.MODELS.read_text())
    out = {"n": n, "drop": drop, "sector": sector, "form": form, "t": t}
    try:
        inp = sy.inputs(_params(n, drop=drop, sector=sector, form=form, t=t))
    except Exception as e:  # noqa: BLE001 -- reported
        out["skip"] = f"{type(e).__name__}: {e}"[:200]
        return out
    if not inp.own:
        out["skip"] = "no own UE"
        return out
    for L in LANES:
        p = dataclasses.replace(inp.p, L=L)
        S = sy.LtbfSystem(dataclasses.replace(inp, p=p))
        cfg, timing = js.fitted(S, model)
        r = js.JobSim(dataclasses.replace(cfg, lt_only=True), timing).run()
        out[f"L{L}"] = {"t_lt": r["t_lt"] * F_HOST, **r["busy"]}
    return out


def _rt_case(args: tuple) -> dict:
    """One (array, L): the shortest symbol period (cycles) with no overrun, by bisection."""
    n, L = args
    from examples.ltbf_dse.hw import jobsim as js
    from examples.ltbf_dse.hw import models as hm
    from examples.ltbf_dse.hw import system as sy

    model = json.loads(hm.MODELS.read_text())
    cfg, timing = js.fitted(sy.LtbfSystem(sy.inputs(_params(n, L=L))), model)
    hi = shortest_period(cfg, timing, n)
    return {"n": n, "L": L, "period_cycles": hi, "re_per_s": 792 / (hi / F_HOST)}


def shortest_period(cfg, timing, n: int) -> float:
    """The shortest symbol period (cycles, within 2%) the slot sustains over :data:`RT_SLOTS` slots
    after the long-term update, by bisection."""
    from examples.ltbf_dse.hw import jobsim as js

    def ok(period_cycles: float) -> bool:
        r = js.JobSim(dataclasses.replace(cfg, sym_period=period_cycles / F_HOST, n_slots=RT_SLOTS),
                      timing).run()  # fmt: skip
        return js.sustained(r, period_cycles)

    lo, hi = 1_000.0, 250_000.0 * n * n / 64
    while not ok(hi):
        hi *= 2
    while hi / lo > 1.02:
        mid = math.sqrt(lo * hi)
        lo, hi = (lo, mid) if ok(mid) else (mid, hi)
    return hi


def _pool():
    os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
    os.environ.setdefault("OMP_NUM_THREADS", "1")
    return mp.get_context("spawn").Pool(4)


def time_tables() -> tuple[list[dict], list[dict]]:
    cases = list(itertools.product(ARRAYS, DROPS, SECTORS, FORMS, TS))
    with _pool() as pool:
        lt = pool.map(_lt_case, cases, chunksize=1)
        rt = pool.map(_rt_case, list(itertools.product(ARRAYS, LANES)), chunksize=1)
    return lt, rt


def t_lt_table(lt: list[dict]) -> dict:
    """``{(n, L, form, t): (T_LT cycles, its busy cycles)}``: the three sectors back to back, the
    largest over the drops."""
    out: dict = {}
    for n, L, form, t in itertools.product(ARRAYS, LANES, FORMS, TS):
        per_drop = []
        for drop in DROPS:
            rows = [
                r
                for r in lt
                if (r["n"], r["drop"], r["form"], r["t"]) == (n, drop, form, t)
                and f"L{L}" in r
            ]
            tot: dict = {}
            for r in rows:
                for k, v in r[f"L{L}"].items():
                    tot[k] = tot.get(k, 0.0) + v
            if tot:
                per_drop.append(tot)
        if per_drop:
            out[n, L, form, t] = max(per_drop, key=lambda x: x["t_lt"])
    return out


# -- cost -------------------------------------------------------------------------------------------


@cache
def _model() -> dict:
    from examples.ltbf_dse.hw import models as hm

    return json.loads(hm.MODELS.read_text())


@cache
def _hub() -> dict:
    from waveflow.utils.csynthparse import CsynthParser

    cp = CsynthParser(
        report_path=str(HERE.parent / "ltbf_hub_proj" / "solution1" / "syn" / "report")
    )
    cp.get_total_resources()
    return cp.total_resources


@cache
def _mem(n: int, form: str) -> tuple[int, int]:
    """Shared Mem's words in use and the span the system builds (a power of two)."""
    from examples.ltbf_dse.hw import system as sy

    S = sy.LtbfSystem(sy.inputs(_params(n, form=form)))
    return S.mem_words, S.mem_span


def cost(n: int, R: int, C: int, W: int, L: int, form: str) -> dict:
    from examples.ltbf_dse.hw import models as hm
    from examples.ltbf_dse.hw import space as sp

    m = _model()
    nrx = n * n
    parts = (
        sp.sys_part(nrx, R=R, C=C, W=W),
        sp.vec_part(nrx, L=L, W=W),
        sp.ing_part(nrx),
    )
    res = {
        c: sum(hm.predict_resources(m, p)[c] for p in parts) + _hub().get(c, 0)
        for c in hm.COUNTERS
    }
    words, span = _mem(n, form)
    res["BRAM_18K"] += 2 * math.ceil(span / BRAM36_WORDS)
    frac = {c: res[c] / AVAILABLE[c] for c in AVAILABLE}
    frac["max"] = max(frac.values())
    return {
        "res": res,
        "frac": frac,
        "mem_words": words,
        "mem_span": span,
        "model_only": frac["max"] > 1,
        "uram_alt": math.ceil(span / URAM_WORDS),
    }


# -- loss -------------------------------------------------------------------------------------------


@cache
def _losses() -> dict:
    out = {}
    with (PAPER / "accuracy_losses.csv").open() as fh:
        for r in csv.DictReader(fh):
            out[r["array"], r["target"], r["config"], int(r["t"]), int(r["r"])] = (
                float(r["median_loss_db"]),
                float(r["p10_loss_db"]),
                float(r["p90_loss_db"]),
            )
    return out


def loss(n: int, target: str, W: int, form: str, t: int) -> tuple[float, ...] | None:
    """(median, 10th percentile -- the best UEs, 90th -- the worst) per-UE loss, dB."""
    return _losses().get(
        (f"{n}x{n}", target, f"W{W}g1l16{'e' if form == 'explicit' else 'f'}", t, 1)
    )


# -- the joint table, decisions, frontiers ----------------------------------------------------------


def designs(times: dict, rt: list[dict]) -> list[dict]:
    rate = {(r["n"], r["L"]): r for r in rt}
    out = []
    for n, (R, C), W, L, form, t, target in itertools.product(
        ARRAYS, SYSTOLIC_SHAPES, WIDTHS, LANES, FORMS, TS, TARGETS
    ):
        lo = loss(n, target, W, form, t)
        tm = times.get((n, L, form, t))
        if lo is None or tm is None:
            continue
        c = cost(n, R, C, W, L, form)
        row = {"array": f"{n}x{n}", "target": target, "R": R, "C": C, "W": W, "L": L, "form": form, "t": t,
               "median_loss_db": lo[0], "p10_loss_db": lo[1], "p90_loss_db": lo[2],
               "t_lt_ms": tm["t_lt"] / F_HOST * 1e3,
               "sys_busy": tm["sys"] / tm["t_lt"], "vec_busy": tm["vec"] / tm["t_lt"],
               "port_busy": tm["port"] / tm["t_lt"], "cpu_busy": tm["cpu"] / tm["t_lt"],
               "re_per_s": rate[n, L]["re_per_s"], "model_only": c["model_only"],
               "mem_words": c["mem_words"], "mem_span": c["mem_span"], "uram_alt": c["uram_alt"]}  # fmt: skip
        for k in COSTS:
            row[f"cost_{k}"] = c["frac"][k]
        for k, v in c["res"].items():
            row[k] = round(v, 1)
        out.append(row)
    return out


def _within(r: dict, n: int, target: str, lb: float, tb: float) -> bool:
    """*r* is a design for (n, target) that fits the device and meets both budgets."""
    return (r["array"] == f"{n}x{n}" and r["target"] == target and not r["model_only"]
            and r["median_loss_db"] <= lb and r["t_lt_ms"] <= tb)  # fmt: skip


def decisions(rows: list[dict]) -> list[dict]:
    """AC13's rule on the predicted table: the cheapest design that fits and meets both budgets
    (none: empty)."""
    out = []
    for n, target, lb, tb, cm in itertools.product(
        ARRAYS, TARGETS, LOSS_BUDGETS, TIME_BUDGETS_MS, COSTS
    ):
        ok = [r for r in rows if _within(r, n, target, lb, tb)]
        best = min(
            ok,
            key=lambda r: (r[f"cost_{cm}"], r["cost_max"], r["t_lt_ms"]),
            default=None,
        )
        out.append({"array": f"{n}x{n}", "target": target, "loss_budget_db": lb, "time_budget_ms": tb,
                    "cost_measure": cm, "feasible": len(ok),
                    **({k: best[k] for k in ("R", "C", "W", "L", "form", "t", "median_loss_db", "t_lt_ms",
                                             f"cost_{cm}", "re_per_s", "model_only")} if best else {})})  # fmt: skip
    return out


def frontiers(rows: list[dict]) -> list[dict]:
    """Per (array, target, loss budget, T_LT budget) the non-dominated designs within both budgets
    that fit; budget ``none``: over every design, model-only included (flagged)."""
    keys = ("cost_max", "median_loss_db", "t_lt_ms")
    out = []
    budgets = [("none", "none"), *itertools.product(LOSS_BUDGETS, TIME_BUDGETS_MS)]
    for n, target, (lb, tb) in itertools.product(ARRAYS, TARGETS, budgets):
        if lb == "none":
            rs = [r for r in rows if r["array"] == f"{n}x{n}" and r["target"] == target]
        else:
            rs = [r for r in rows if _within(r, n, target, lb, tb)]
        for r in rs:
            if not any(
                all(o[k] <= r[k] for k in keys) and any(o[k] < r[k] for k in keys)
                for o in rs
            ):
                out.append({"loss_budget_db": lb, "time_budget_ms": tb, **r})
    return out


def _write(name: str, rows: list[dict]) -> Path:
    PAPER.mkdir(parents=True, exist_ok=True)
    path = PAPER / name
    keys = list(dict.fromkeys(k for r in rows for k in r))
    with path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=keys)
        w.writeheader()
        for r in rows:
            w.writerow(
                {k: (f"{v:.6g}" if isinstance(v, float) else v) for k, v in r.items()}
            )
    return path


def main() -> None:
    if (
        "--reuse-time" in sys.argv
    ):  # the long-term table from the last run; the slot's rate afresh
        from examples.ltbf_dse.hw.fidelity import _load_time

        lt = _load_time("dse_time.csv")
        with _pool() as pool:
            rt = pool.map(_rt_case, list(itertools.product(ARRAYS, LANES)), chunksize=1)
    else:
        lt, rt = time_tables()
    _write("dse_time.csv", [{k: v for k, v in r.items() if not isinstance(v, dict)}
                            | {f"{L}_{k}": v for L in ("L2", "L4") if L in r for k, v in r[L].items()}
                            for r in lt])  # fmt: skip
    _write("dse_realtime.csv", rt)
    rows = designs(t_lt_table(lt), rt)
    for name, table in (
        ("dse_designs.csv", rows),
        ("dse_decisions.csv", decisions(rows)),
        ("dse_frontiers.csv", frontiers(rows)),
    ):
        print(_write(name, table), len(table))


if __name__ == "__main__":
    sys.exit(main())
