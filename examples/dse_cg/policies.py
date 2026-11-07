"""policies.py -- scripted explorers, one per arm: the agent-free comparison (plan step 6).

They drive the service through the same ``dselib`` calls an agent makes, so their ledgers are scored by
the same ``score.py``.  The vision asks that the core result not depend on an agent (an agent adds
variance); these give that result, and the agent runs show the advantage survives an autonomous
explorer.

Both judge a design by the objective's own rules (``score.feasible``, ``score.objective_value``) on the
metrics their calls returned: synthesized ``lut``/``ff``/``dsp``/``bram``, ``t_upd_ms`` at each ``t``,
``median_loss_db`` at each accuracy workload.
"""

from __future__ import annotations

import itertools
from typing import Any, Callable

from examples.dse_cg.score import feasible, objective_value


def designs(lib) -> list[dict]:
    """Every design of the space: the product of its design parameters' values, the illegal ones
    dropped by asking ``golden`` (an illegal point is a row at no cost)."""
    sp = lib.space()
    names = [p["name"] for p in sp["parameters"] if p["kind"] == "design"]
    values = [next(p["values"] for p in sp["parameters"] if p["name"] == n) for n in names]
    out = []
    for combo in itertools.product(*values):
        p = dict(zip(names, combo, strict=True))
        row = lib.golden(p, "accuracy")
        if not str(row.get("status", "")).startswith("illegal"):
            out.append(p)
    return out


def _workloads(objective: dict) -> tuple[list[dict], list[dict]]:
    """The objective's timing and accuracy workloads, each as ``{"name", ...}``."""
    cons = list(objective.get("subject_to", []))
    if objective.get("workload"):
        cons.append({"workload": objective["workload"]})
    timing, accuracy = [], []
    for c in cons:
        w = c.get("workload")
        if w is None:
            continue
        w = ({"name": w} if isinstance(w, str) else dict(w)) | c.get("at", {})
        (accuracy if w["name"] == "accuracy" else timing).append(w)
    return timing, accuracy


def _key(p: dict) -> tuple:
    return tuple(sorted(p.items()))


def _accuracy(m: dict, row: dict, w: dict) -> None:
    if row.get("median_loss_db") is not None:
        m[("median_loss_db", w.get("t", 3), w.get("target", "-6_14"))] = float(row["median_loss_db"])


def _timing(m: dict, row: dict, w: dict) -> None:
    if row.get("t_upd_ms") is not None:
        m[("t_upd_ms", w.get("t", 3))] = float(row["t_upd_ms"])


def _verify(lib, batch: list[dict], timing: list[dict], measured: dict) -> None:
    """Synthesize and RTL-simulate *batch* on every timing workload; their metrics into *measured*."""
    for row in lib.wait(lib.synth(batch)):
        if row.get("status") == "ok":
            m = measured.setdefault(_key(row["params"]), {})
            m.update({r: row[r] for r in ("lut", "ff", "dsp", "bram") if r in row})
    for w in timing:
        for row in lib.wait(lib.rtlsim(batch, w)):
            if row.get("status") in ("ok", "mismatch"):
                _timing(measured.setdefault(_key(row["params"]), {}), row, w)


def _best(measured: dict, objective: dict) -> tuple[dict | None, Any]:
    best, val = None, None
    for k, m in measured.items():
        if feasible(m, objective):
            v = objective_value(m, objective)
            if v is not None and (val is None or v < val):
                best, val = dict(k), v
    return best, val


def _submit(lib, best: dict | None, val, how: str) -> dict:
    if best is None:
        return {"submitted": None, "reason": "no verified design met the constraints"}
    out = lib.submit(best, {"evidence": [], "how": how, "objective_value": val})
    return {"submitted": best, "value": val, **out}


def traditional(lib, objective: dict, analytic_cost: Callable[[dict], float], k: int = 2) -> dict:
    """The systems-team + hardware-team flow.

    1. Sweep accuracy with ``lib.golden`` over the design points that affect it; keep the points that
       meet the accuracy constraints.
    2. Rank them by ``analytic_cost(params)``: a written estimate of the objective (for example LUTs
       from multiplier counts, cycles from trip counts), the spreadsheet a hardware team keeps.
    3. ``synth`` + ``rtlsim`` them in that order, a batch of ``k`` at a time, until the constraints
       are met and the best verified objective has not improved for one batch.
    4. ``submit`` the best verified design.
    """
    timing, accuracy = _workloads(objective)
    measured: dict[tuple, dict] = {}
    cands = []
    for p in designs(lib):
        m = measured.setdefault(_key(p), {})
        for w in accuracy:
            _accuracy(m, lib.golden(p, w), w)
        acc_only = {**objective, "subject_to": [c for c in objective.get("subject_to", [])
                                                 if c.get("workload") == "accuracy"]}
        if feasible(m, acc_only):
            cands.append(p)
    cands.sort(key=analytic_cost)
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
    best, val = _best(measured, objective)
    return _submit(lib, best, val, "traditional")


def waveflow(lib, objective: dict, top_k: int = 2) -> dict:
    """The same flow with pysim.

    1. ``lib.pysim`` every point of the space (accuracy exact; cycles and resources predicted).
    2. Keep the predicted-feasible points; rank them by the predicted objective.
    3. ``synth`` + ``rtlsim`` the top ``top_k``; while a measurement disagrees with its prediction
       enough to change the ranking, widen to the next ``top_k``.
    4. ``submit`` the best verified design.
    """
    timing, accuracy = _workloads(objective)
    predicted: dict[tuple, dict] = {}
    for p in designs(lib):
        m = predicted.setdefault(_key(p), {})
        for w in timing or [{"name": "long_term"}]:
            row = lib.pysim(p, w)
            _timing(m, row, w)
            m.update({r: v for r, v in (row.get("resources") or {}).items() if r in ("lut", "ff", "dsp", "bram")})
        for w in accuracy:
            _accuracy(m, lib.golden(p, w), w)
    ranked = sorted((k for k, m in predicted.items() if feasible(m, objective)),
                    key=lambda k: objective_value(predicted[k], objective))
    measured: dict[tuple, dict] = {}
    i = 0
    while i < len(ranked):
        batch = [dict(k) for k in ranked[i:i + top_k]]
        _verify(lib, batch, timing, measured)
        for k in ranked[i:i + top_k]:            # accuracy is exact: carry it over
            measured.setdefault(k, {}).update({x: v for x, v in predicted[k].items() if isinstance(x, tuple)
                                               and x[0] == "median_loss_db"})
        i += top_k
        best, val = _best(measured, objective)
        # widen while the next predicted design could still beat the best measured one
        if best is not None and i < len(ranked) and objective_value(predicted[ranked[i]], objective) >= val:
            break
    best, val = _best(measured, objective)
    return _submit(lib, best, val, "waveflow")
