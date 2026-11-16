"""deadline.py -- the tight-deadline objectives: an exploratory set, not the pre-registration.

**Chosen on 2026-11-12, after G1 was measured and after a scripted sweep over the deadline had been
examined** (plan ``plans/dse_cg.md``, progress log): they are not pre-registered, and every result
from them is exploratory and is reported as such.  The pre-registered objectives (``prereg.OBJECTIVES``)
are unchanged and stay the primary result.

Why they exist.  In the three pre-registered objectives the design with the fewest resources that
meets the accuracy limit (68.5k LUT, 116 DSP) already meets the update-time limit, so the time limit
never decides the answer and a resource ranking finds the optimum.  These objectives tighten the limit
to the fastest 5-20 % of the grid (G1: ``T_upd`` at ``t`` 3 from 3.39 to 6.82 ms), where the update
time decides which designs are feasible.  Each is "minimize LUT (or DSP) subject to ``T_upd`` <= x at
``t`` 3 and median loss <= 0.1 dB at the [-6, 3] dB targets", for x in :data:`DEADLINES_MS`.

Agents run on them only as pilots (``run_agent.py --pilot``); a scored run refuses them.

    python -m examples.dse_cg.deadline scripted --out ~/ali/dse_cg/records/deadline_scripted
    python -m examples.dse_cg.deadline score --runs ~/ali/dse_cg/runs_deadline/pilot \\
        --scripted ~/ali/dse_cg/records/deadline_scripted --out summary.json
"""

from __future__ import annotations

from pathlib import Path

#: The update-time limits, ms at ``t`` 3: the fastest 5 %, 10 % and 20 % of G1's designs (about).
DEADLINES_MS = (3.45, 3.55, 3.65)

#: The date these objectives were chosen (after G1 and the scripted deadline sweep).
CHOSEN = "2026-11-12"

_NAMES = {"lut": "LUTs", "dsp": "DSPs"}


def _objective(res: str, x: float) -> dict:
    return {
        "id": f"{res}_tupd{x:.2f}_loss01",
        "text": f"Minimize {_NAMES[res]} subject to T_upd <= {x:.2f} ms (t = 3) and median SINR loss "
                f"<= 0.1 dB at SINR targets [-6, 3] dB.",
        "minimize": res,
        "subject_to": [
            {"metric": "t_upd_ms", "workload": "long_term", "at": {"t": 3}, "op": "<=", "value": x},
            {"metric": "median_loss_db", "workload": "accuracy", "at": {"t": 3, "target": "-6_3"},
             "op": "<=", "value": 0.1},
        ],
    }


#: The exploratory objectives, LUT then DSP, loosest last.
OBJECTIVES = [_objective(r, x) for r in ("lut", "dsp") for x in DEADLINES_MS]


def scripted(table: Path, out: Path, budget_s: float | None = None) -> list[Path]:
    """Both scripted policies (``policies.run_scripted``) on every objective, one run each."""
    from examples.dse_cg import policies, prereg

    budget = prereg.BUDGET_S if budget_s is None else budget_s
    return [policies.run_scripted(arm, o, table, Path(out) / arm / o["id"] / "scripted", budget_s=budget,
                                  k_workers=prereg.K_WORKERS)
            for o in OBJECTIVES for arm in ("traditional", "waveflow")]


def score(record_dirs: list[Path], table: Path, epsilon: float | None = None) -> dict:
    """``score.summarize`` over *record_dirs*, and every run's own measures (its anytime curve, time
    to target, spend, turns and cost), with the calibration charged as the pre-registration says."""
    from examples.dse_cg import grid, prereg
    from examples.dse_cg import score as sc

    eps = prereg.EPSILON if epsilon is None else epsilon
    truth = sc.Truth(table)
    designs = grid.designs(truth.design)
    summary = sc.summarize(record_dirs, truth, OBJECTIVES, designs, eps, calibration_s=prereg.CALIBRATION_S)
    by_id = {o["id"]: o for o in OBJECTIVES}
    runs = []
    for d in record_dirs:
        import json

        rec = json.loads((Path(d) / "record.json").read_text(encoding="utf-8"))
        opt = summary["optimum"][rec["objective"]].get("value")
        m = sc.run_measures(Path(d), truth, by_id[rec["objective"]], opt, eps)
        m["turns"] = (rec.get("result") or {}).get("num_turns")
        dur = (rec.get("result") or {}).get("duration_ms")
        m["active_s"] = None if dur is None else dur / 1000     # the session's own time, to its result
        m["stopped"] = rec.get("stopped")
        m["cost_usd"] = (rec.get("usage") or {}).get("total_cost_usd")
        runs.append(m)
    return {"summary": summary, "runs": runs, "objectives": OBJECTIVES, "epsilon": eps,
            "calibration_s": prereg.CALIBRATION_S, "budget_s": prereg.BUDGET_S, "chosen": CHOSEN,
            "hardness": {o["id"]: hardness(truth, o, designs) for o in OBJECTIVES + prereg.OBJECTIVES}}


def hardness(truth, objective: dict, designs: list[dict]) -> dict:
    """How much the deadline decides, from the measured grid: the designs that meet the accuracy
    limit, those that also meet the deadline, and those that use fewer resources than the optimum
    but miss the deadline -- each one an RTL run for an explorer with no timing model to rule out."""
    from examples.dse_cg import score as sc

    res = objective["minimize"]
    if res not in ("lut", "dsp", "ff", "bram"):
        return {}
    acc = {"subject_to": [c for c in objective["subject_to"] if c["metric"] != "t_upd_ms"]}
    ms = [m for m in (truth.measured(p) for p in designs) if m and sc.feasible(m, acc)]
    feas = [m for m in ms if sc.feasible(m, objective)]
    opt = min(m[res] for m in feas) if feas else None
    least = min(ms, key=lambda m: m[res]) if ms else None
    limit = next(c for c in objective["subject_to"] if c["metric"] == "t_upd_ms")
    return {"accurate": len(ms), "feasible": len(feas), "optimum": opt,
            "cheaper_infeasible": sum(1 for m in ms if opt is not None and m[res] < opt
                                      and not sc.feasible(m, objective)),
            "least": None if least is None else least[res],
            "least_t_upd_ms": None if least is None else sc._metric(least, limit)}


def main(argv: list[str] | None = None) -> int:
    import argparse
    import json

    from examples.dse_cg.grid import TABLE

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    a1 = sub.add_parser("scripted", help="both scripted policies on every objective")
    a1.add_argument("--out", type=Path, required=True)
    a2 = sub.add_parser("score", help="score agent runs (and scripted ones) into one JSON")
    a2.add_argument("--runs", type=Path, nargs="*", default=[], help="roots holding <arm>/<objective>/seedNN")
    a2.add_argument("--scripted", type=Path, nargs="*", default=[], help="roots of `scripted` records")
    a2.add_argument("--out", type=Path, required=True)
    for a in (a1, a2):
        a.add_argument("--table", type=Path, default=TABLE)
    a = ap.parse_args(argv)
    if a.cmd == "scripted":
        for d in scripted(a.table, a.out):
            print(d, flush=True)
        return 0
    ids = {o["id"] for o in OBJECTIVES}
    dirs = [d for root in a.runs for d in sorted(root.glob("*/*/seed*")) if d.parent.name in ids]
    dirs += [d for root in a.scripted for d in sorted(root.glob("*/*/scripted")) if d.parent.name in ids]
    a.out.write_text(json.dumps(score(dirs, a.table), indent=1, default=str), encoding="utf-8")
    print(a.out, len(dirs), "records")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
