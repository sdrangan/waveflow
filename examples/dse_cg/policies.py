"""policies.py -- scripted explorers, one per arm: the agent-free comparison (plan step 6).

They drive the service through the same ``dselib`` calls an agent makes, so their ledgers are scored by
the same ``score.py``.  The vision asks that the core result not depend on an agent (an agent adds
variance); these give that result, and the agent runs show the advantage survives an autonomous
explorer.

Both judge a design by the objective's own rules (``score.feasible``, ``score.objective_value``) on the
metrics their calls returned: synthesized ``lut``/``ff``/``dsp``/``bram``, ``t_upd_ms`` at each ``t``,
``median_loss_db`` at each accuracy workload.  Both know the space as ``space()`` states it (its
parameters and its legality rules) and nothing measured beforehand.

    python -m examples.dse_cg.policies --table <G1 table> --out <records root>
    python -m examples.dse_cg.policies --table <G1 table> --out <records root> --uncalibrated

runs both policies on every objective on the replay backend and writes one record each (``score.py``'s
layout); ``score.summarize`` over them is step 6's result.  ``--uncalibrated`` runs the traditional
policy with its ``hls_estimate`` uncorrected, the rule as first written (the ablation; plan row of
2026-11-12).
"""

from __future__ import annotations

import itertools
from collections.abc import Callable
from pathlib import Path
from typing import Any

from examples.dse_cg.score import feasible, objective_value, workloads

RESOURCES = ("lut", "ff", "dsp", "bram")


def designs(lib) -> list[dict]:
    """Every legal design of the space: the product of its design parameters' values, less the points
    a ``space()`` constraint rules out (each rule a Python expression over the parameters)."""
    sp = lib.space()
    names = [p["name"] for p in sp["parameters"] if p["kind"] == "design"]
    values = [next(p["values"] for p in sp["parameters"] if p["name"] == n) for n in names]
    rules = [c["rule"] for c in sp.get("constraints", [])]
    out = []
    for combo in itertools.product(*values):
        p = dict(zip(names, combo, strict=True))
        if all(eval(r, {"__builtins__": {}}, dict(p)) for r in rules):     # the space's own rules
            out.append(p)
    return out


def _key(p: dict) -> tuple:
    return tuple(sorted(p.items()))


def _accuracy(m: dict, row: dict, w: dict) -> None:
    if row.get("median_loss_db") is not None:
        m[("median_loss_db", w.get("t", 3), w.get("target", "-6_14"))] = float(row["median_loss_db"])


def _timing(m: dict, row: dict, w: dict) -> None:
    if row.get("t_upd_ms") is not None:
        m[("t_upd_ms", w.get("t", 3))] = float(row["t_upd_ms"])


def _verify(lib, batch: list[dict], timing: list[dict], measured: dict) -> None:
    """Synthesize *batch*, then RTL-simulate it on every timing workload -- a job a point, so the
    service's worker slots run them side by side; their metrics into *measured*."""
    for row in lib.wait([lib.synth([p]) for p in batch]):
        if row.get("status") == "ok":
            m = measured.setdefault(_key(row["params"]), {})
            m.update({r: row[r] for r in RESOURCES if r in row})
    for row in lib.wait([lib.rtlsim([p], w) for w in timing for p in batch]):
        if row.get("status") in ("ok", "mismatch"):
            _timing(measured.setdefault(_key(row["params"]), {}), row, row.get("workload") or {})


def _best(measured: dict, objective: dict) -> tuple[dict | None, Any]:
    best, val = None, None
    for k, m in measured.items():
        if feasible(m, objective):
            v = objective_value(m, objective)
            if v is not None and (val is None or v < val):
                best, val = dict(k), v
    return best, val


def _evidence(m: dict, objective: dict, timing: list[dict], accuracy: list[dict]) -> list[dict]:
    """The report's evidence: each constraint's and the objective's metric, with its source."""
    ev = []
    for r in RESOURCES:
        if r in m and (r == objective["minimize"] or any(c["metric"] == r for c in objective.get("subject_to", []))):
            ev.append({"metric": r, "value": m[r], "source": "synth"})
    for w in timing:
        v = m.get(("t_upd_ms", w.get("t", 3)))
        if v is not None:
            ev.append({"metric": "t_upd_ms", "value": v, "workload": w, "source": "rtlsim"})
    for w in accuracy:
        v = m.get(("median_loss_db", w.get("t", 3), w.get("target", "-6_14")))
        if v is not None:
            ev.append({"metric": "median_loss_db", "value": v, "workload": w, "source": "golden"})
    return ev


def _submit(lib, measured: dict, objective: dict, how: str) -> dict:
    timing, accuracy = workloads(objective)
    best, val = _best(measured, objective)
    if best is None:
        return {"submitted": None, "reason": "no verified design met the constraints"}
    report = {"params": best, "evidence": _evidence(measured[_key(best)], objective, timing, accuracy),
              "rationale": how}
    try:
        out = lib.submit(best, report)
    except Exception as e:  # noqa: BLE001 -- a refused submit (the budget spent) ends the run too
        out = {"accepted": False, "reason": str(e)}
    return {"submitted": best, "value": val, **out}


def _refused(e: Exception) -> bool:
    """A refusal from the service (``dselib.Refused``): here, the budget spent."""
    return type(e).__name__ == "Refused"


def speed_rank(p: dict) -> tuple:
    """The traditional team's ordering for speed, fastest first, from the architecture alone: a
    faster memory first (the example's own finding: the update is memory-bound), then more banks, a
    larger array, more lanes, a wider CG window."""
    return (p["mem_rate"], -p["banks"], -p["R"] * p["C"], -p["L"], -p["cg_window"])


def rank_traditional(lib, objective: dict, cands: list[dict], calibrate: bool = True) -> list[dict]:
    """The hardware team's ranking, without pysim.

    A resource objective (``lut``, ``ff``, ``dsp``, ``bram``): ``hls_estimate`` every candidate (one
    csynth a kernel identity, the quick look a team takes), ranked by its kernels' total of that
    resource, ties broken by :func:`speed_rank`.  A timing objective: :func:`speed_rank`.

    A resource constraint is applied to the kernels' totals, corrected first: the team synthesizes the
    candidate it would verify first and scales each resource's estimate by measured / estimated on it
    (csynth's LUT estimate of these kernels is about 1.9x their synthesis, its BRAM about 0.2x; plan
    row of 2026-11-12), and verification has the last word.  *calibrate* False applies the constraint
    to the totals as they are, the rule as first written (step 6's ablation: there a LUT constraint
    removes every design)."""
    res_cons = [c for c in objective.get("subject_to", []) if c["metric"] in RESOURCES]
    if not (objective["minimize"] in RESOURCES or res_cons):
        return sorted(cands, key=speed_rank)
    est: dict[tuple, dict] = {}
    for row in lib.wait([lib.hls_estimate([p]) for p in cands]):
        if row.get("status") == "ok":
            est[_key(row["params"])] = {r: sum(float(k.get(r) or 0) for k in row["kernels"].values())
                                        for r in RESOURCES}
    cands = [p for p in cands if _key(p) in est]
    r0 = objective["minimize"] if objective["minimize"] in RESOURCES else None
    order = sorted(cands, key=lambda p: (est[_key(p)][r0], speed_rank(p))) if r0 else sorted(cands, key=speed_rank)
    scale: dict[str, float] = {}
    if res_cons and calibrate and order:
        m, e = lib.wait([lib.synth([order[0]])])[0], est[_key(order[0])]
        if m.get("status") == "ok":
            scale = {r: float(m[r]) / e[r] for r in RESOURCES if e[r] and m.get(r) is not None}
    return [p for p in order
            if feasible({r: v * scale.get(r, 1.0) for r, v in est[_key(p)].items()}, {"subject_to": res_cons})]


def traditional(lib, objective: dict, rank: Callable[[Any, dict, list[dict]], list[dict]] | None = None,
                k: int = 2) -> dict:
    """The systems-team + hardware-team flow.

    1. Sweep accuracy with ``lib.golden`` over the design space; keep the points that meet the
       accuracy constraints.
    2. Rank them (:func:`rank_traditional` unless *rank* is given): the estimate a hardware team
       keeps -- ``hls_estimate`` for resources, the architecture for speed.
    3. ``synth`` + ``rtlsim`` them in that order, a batch of ``k`` at a time, until the constraints
       are met and the best verified objective has not improved for one batch.
    4. ``submit`` the best verified design.
    """
    timing, accuracy = workloads(objective)
    measured: dict[tuple, dict] = {}
    acc_only = {"subject_to": [c for c in objective.get("subject_to", [])
                               if not c["metric"].startswith("t_") and c["metric"] not in RESOURCES]}
    try:
        cands = []
        for p in designs(lib):
            m = measured.setdefault(_key(p), {})
            for w in accuracy:
                _accuracy(m, lib.golden(p, w), w)
            if feasible(m, acc_only):
                cands.append(p)
        cands = (rank or rank_traditional)(lib, objective, cands)
        best_val, stale = None, 0
        for i in range(0, len(cands), k):
            _verify(lib, cands[i:i + k], timing, measured)
            _b, val = _best(measured, objective)
            if val is not None and (best_val is None or val < best_val):
                best_val, stale = val, 0
            elif best_val is not None:
                stale += 1
                if stale >= 1:
                    break
    except Exception as e:
        if not _refused(e):
            raise
    return _submit(lib, measured, objective, "traditional: golden sweep, hls_estimate / architecture "
                                             "ranking, verified in batches until no improvement")


def waveflow(lib, objective: dict, top_k: int = 2) -> dict:
    """The same flow with pysim.

    1. ``lib.pysim`` every point of the space on each timing workload (accuracy exact; cycles and
       resources predicted) and ``lib.golden`` on each accuracy workload.
    2. Keep the predicted-feasible points; rank them by the predicted objective.
    3. ``synth`` + ``rtlsim`` the top ``top_k``; while the next predicted design could still beat the
       best measured one, widen to the next ``top_k``.
    4. ``submit`` the best verified design.
    """
    timing, accuracy = workloads(objective)
    predicted: dict[tuple, dict] = {}
    measured: dict[tuple, dict] = {}
    try:
        for p in designs(lib):
            m = predicted.setdefault(_key(p), {})
            for w in timing or [{"name": "long_term"}]:
                row = lib.pysim(p, w)
                _timing(m, row, w)
                m.update({r: v for r, v in (row.get("resources") or {}).items() if r in RESOURCES})
            for w in accuracy:
                _accuracy(m, lib.golden(p, w), w)
        ranked = sorted((k for k, m in predicted.items() if feasible(m, objective)),
                        key=lambda k: objective_value(predicted[k], objective))
        i = 0
        while i < len(ranked):
            batch = [dict(k) for k in ranked[i:i + top_k]]
            _verify(lib, batch, timing, measured)
            for k in ranked[i:i + top_k]:            # accuracy is exact: carry it over
                measured.setdefault(k, {}).update({x: v for x, v in predicted[k].items()
                                                   if isinstance(x, tuple) and x[0] == "median_loss_db"})
            i += top_k
            best, val = _best(measured, objective)
            # widen while the next predicted design could still beat the best measured one
            if best is not None and i < len(ranked) and objective_value(predicted[ranked[i]], objective) >= val:
                break
    except Exception as e:
        if not _refused(e):
            raise
    return _submit(lib, measured, objective, "waveflow: pysim over the space, golden for accuracy, the "
                                             "predicted best verified until no predicted design could beat it")


def run_scripted(arm: str, objective: dict, table: Path, out_dir: Path, budget_s: float | None = None,
                 k_workers: int = 2, design: Any = None, backend: Any = None, calibrate: bool = True) -> Path:
    """One scripted run on the replay backend over G1's *table*, recorded in *out_dir* as an agent run
    is (``record.json`` and ``ledger.jsonl``, ``score.py``'s layout).  The ledger starts empty.
    *calibrate*: the traditional rule's, :func:`rank_traditional`."""
    import functools
    import json
    import time

    from examples.dse_cg import service as sv
    from examples.dse_cg.skill.dse import dselib

    if design is None:
        from examples.dse_cg.ltbf_cg_mem import LtbfCgMemAdaptor

        design = LtbfCgMemAdaptor()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    ledger = out_dir / "ledger.jsonl"
    ledger.unlink(missing_ok=True)
    s = sv.Service(design, arm, objective, budget_s, k_workers,
                   backend or sv.ReplayBackend(design, table), ledger)
    dselib.bind(s)
    t0 = time.perf_counter()
    try:
        out = traditional(dselib, objective, functools.partial(rank_traditional, calibrate=calibrate)) \
            if arm == "traditional" else waveflow(dselib, objective)
    finally:
        dselib.bind(None)
    rec = {"arm": arm, "objective": objective["id"], "agent": "scripted", "seed": 0, "void": False,
           "budget_s": budget_s, "k_workers": k_workers, "wall_s": time.perf_counter() - t0,
           "outcome": out, "table": str(table)}
    if arm == "traditional":
        rec["calibrate"] = calibrate
    (out_dir / "record.json").write_text(json.dumps(rec, indent=1, default=str), encoding="utf-8")
    return out_dir


def main(argv: list[str] | None = None) -> int:
    import argparse
    import json

    from examples.dse_cg import prereg

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--table", type=Path, required=True, help="G1's table (grid.py table + finish)")
    ap.add_argument("--out", type=Path, required=True, help="the records' root")
    ap.add_argument("--objective", action="append", help="an objective id (default: every one)")
    ap.add_argument("--uncalibrated", action="store_true",
                    help="the traditional policy only, its estimate uncorrected (the rule as first written; "
                         "step 6's ablation), into scripted_uncalibrated/")
    a = ap.parse_args(argv)
    arms, sub = (("traditional",), "scripted_uncalibrated") if a.uncalibrated else (("traditional", "waveflow"), "scripted")
    for o in prereg.OBJECTIVES:
        if a.objective and o["id"] not in a.objective:
            continue
        for arm in arms:
            d = run_scripted(arm, o, a.table, a.out / arm / o["id"] / sub, prereg.BUDGET_S,
                             prereg.K_WORKERS, calibrate=not a.uncalibrated)
            print(arm, o["id"], json.loads((d / "record.json").read_text())["outcome"], flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
