"""score.py -- turn run records into the paper's numbers (plan ``plans/dse_cg.md``, *Measurements*).

Reads only ledgers, reports and the ground-truth table; never re-runs anything.

The ground truth and pysim's scoring against it (plan step 4) are written; the runs' measures
(anytime curves, time to target, report checks, the summary) are plan steps 6 and 7.
"""

from __future__ import annotations

from pathlib import Path


class Truth:
    """The ground-truth table (G1: ``grid.py table`` + ``finish``), read once: each design's measured
    metrics and pysim's predictions of them.  A design is its filled parameters."""

    def __init__(self, truth_dir: Path, design=None):
        import json

        from examples.dse_cg.ltbf_cg_mem import LtbfCgMemAdaptor

        self.dir, self.design = Path(truth_dir), design or LtbfCgMemAdaptor()
        self.t: dict[str, dict] = {}
        for fn in ("synth", "hls", "rtlsim", "golden", "pysim"):
            f = self.dir / f"{fn}.jsonl"
            rows = [json.loads(x) for x in f.read_text(encoding="utf-8").splitlines() if x.strip()] \
                if f.is_file() else []
            self.t[fn] = {r["key"]: r["row"] for r in rows}
        self._ids: dict[str, dict] = {}

    def _key(self, p: dict) -> str:
        import json

        return json.dumps(p, sort_keys=True)

    def identities(self, p: dict) -> dict:
        k = self._key(p)
        if k not in self._ids:
            self._ids[k] = self.design.identities(p)
        return self._ids[k]

    def measured(self, p: dict) -> dict | None:
        """The design's measured metrics, or ``None`` if a part or run is missing: the built parts'
        synthesized ``lut``, ``ff``, ``dsp``, ``bram`` (Shared Mem's block RAM by formula added, as
        the service's rows have it), ``t_upd_ms`` at each ``t``, and ``golden``'s losses."""
        from examples.dse_cg.service import eval_key

        d, out = self.design, {}
        parts = [self.t["synth"].get(i) for i in self.identities(p).values()]
        if any(r is None for r in parts):
            return None
        for r in ("lut", "ff", "dsp", "bram"):
            out[r] = sum(int(x.get(r) or 0) for x in parts)
        out["bram"] += int(d.unbuilt_resources(p).get("bram", 0))
        for name, w in self._workloads("long_term"):
            row = self.t["rtlsim"].get(d.sim_identity(p, {"name": name, **w})["run"])
            if row is not None:                 # a run not measured: its metric is absent
                out[("t_upd_ms", w["t"])] = float(row["t_upd_ms"])
        for name, w in self._workloads("accuracy"):
            row = self.t["golden"].get(eval_key("golden", p, name, w))
            if row is not None:
                out[("median_loss_db", w["t"], w["target"])] = float(row["median_loss_db"])
        return out

    def predicted(self, p: dict) -> dict | None:
        """pysim's prediction of the same metrics (accuracy is exact: ``golden``'s)."""
        from examples.dse_cg.service import eval_key

        out = {}
        for name, w in self._workloads("long_term"):
            row = self.t["pysim"].get(eval_key("pysim", p, name, w))
            if row is None:
                continue
            out[("t_upd_ms", w["t"])] = float(row["t_upd_ms"])
            for r in ("lut", "ff", "dsp", "bram"):
                out[r] = int(row["resources"].get(r) or 0)
            out["parts"] = row.get("parts", {})
        if not out:
            return None
        m = self.measured(p) or {}
        out.update({k: v for k, v in m.items() if isinstance(k, tuple) and k[0] == "median_loss_db"})
        return out

    def _workloads(self, name: str) -> list[tuple[str, dict]]:
        from examples.dse_cg.ltbf_cg_mem import GRID

        if name == "long_term":
            ws = [{"name": name, "t": t} for t in GRID["t"][1]]
        else:
            ws = [{"name": name, "t": t, "target": g} for t in GRID["t"][1] for g in GRID["target"][1]]
        return [self.design.workload(w) for w in ws]


def _metric(m: dict, c: dict, default_t: int = 3):
    """The value of constraint (or objective) *c*'s metric in metrics *m*."""
    w = c.get("workload")
    at = {**c.get("at", {}), **({k: v for k, v in w.items() if k != "name"} if isinstance(w, dict) else {})}
    name = c["metric"] if "metric" in c else c["minimize"]
    if name == "t_upd_ms":
        return m.get(("t_upd_ms", at.get("t", default_t)))
    if name == "median_loss_db":
        return m.get(("median_loss_db", at.get("t", default_t), at.get("target", "-6_14")))
    return m.get(name)


def feasible(m: dict, objective: dict) -> bool:
    """Whether metrics *m* meet every constraint of *objective*."""
    ops = {"<=": lambda a, b: a <= b, ">=": lambda a, b: a >= b, "<": lambda a, b: a < b,
           ">": lambda a, b: a > b}
    for c in objective.get("subject_to", []):
        v = _metric(m, c)
        if v is None or not ops[c["op"]](v, c["value"]):
            return False
    return True


def objective_value(m: dict, objective: dict):
    """The minimized metric's value in metrics *m*."""
    return _metric(m, {"minimize": objective["minimize"], "workload": objective.get("workload")})


def optimum(truth: Truth, objective: dict, designs: list[dict]) -> dict:
    """The ground-truth best feasible design for ``objective`` over *designs* (the grid): exact,
    since the table holds every legal point of the space.  ``{"params", "value"}`` or ``{}``."""
    best: dict = {}
    for p in designs:
        m = truth.measured(p)
        if m is not None and feasible(m, objective):
            v = objective_value(m, objective)
            if v is not None and (not best or v < best["value"]):
                best = {"params": p, "value": v}
    return best


def pysim_pick(truth: Truth, objective: dict, designs: list[dict]) -> dict:
    """The design pysim's predictions make best, and its measured value."""
    best: dict = {}
    for p in designs:
        m = truth.predicted(p)
        if m is not None and feasible(m, objective):
            v = objective_value(m, objective)
            if v is not None and (not best or v < best["predicted"]):
                best = {"params": p, "predicted": v}
    if best:
        mm = truth.measured(best["params"]) or {}
        best["value"] = objective_value(mm, objective)
        best["feasible"] = feasible(mm, objective)
    return best


def score_pysim(truth: Truth, designs: list[dict], objectives: list[dict], exclude: list[dict]) -> dict:
    """pysim against G1 as ``g1_prereg.SCORING`` states it, over *designs* less *exclude* (the
    integration points): per built part, DSP and BRAM exact and LUT and FF mean absolute relative
    error; per run, cycles' mean absolute relative error; per objective, whether pysim's pick is within
    the stated share of the optimum."""
    import json

    import numpy as np

    from examples.dse_cg.g1_prereg import SCORING

    skip = {json.dumps(truth.design.fill(p), sort_keys=True) for p in exclude}
    parts: dict[str, dict] = {}
    runs: list[float] = []
    for p in designs:
        if json.dumps(p, sort_keys=True) in skip:
            continue
        pred = truth.predicted(p)
        meas = truth.measured(p)
        if pred is None or meas is None:
            continue
        for node, ident in truth.identities(p).items():
            syn = truth.t["synth"].get(ident)
            est = (pred.get("parts", {}).get(node) or {}).get("integrated")
            if syn is not None and est is not None:
                parts.setdefault(ident, {"node": node, "measured": syn, "predicted": est})
        for k, v in meas.items():
            if isinstance(k, tuple) and k[0] == "t_upd_ms" and pred.get(k):
                runs.append(abs(pred[k] / v - 1))
    out: dict = {"parts": len(parts), "runs": len(runs)}
    for r in ("dsp", "bram", "lut", "ff"):
        errs = [(e["predicted"][r], int(e["measured"].get(r) or 0)) for e in parts.values()]
        rule = SCORING[r]
        if rule["rule"] == "exact":
            miss = [e for e in errs if e[0] != e[1]]
            out[r] = {"exact": len(errs) - len(miss), "of": len(errs), "pass": not miss}
        else:
            rel = [abs(a / b - 1) for a, b in errs if b]
            mean = float(np.mean(rel)) if rel else None
            out[r] = {"mean_abs_rel": mean, "pass": mean is not None and mean <= rule["max"]}
    mean = float(np.mean(runs)) if runs else None
    out["cycles"] = {"mean_abs_rel": mean, "pass": mean is not None and mean <= SCORING["cycles"]["max"]}
    picks = []
    for o in objectives:
        best, pick = optimum(truth, o, designs), pysim_pick(truth, o, designs)
        ok = bool(best) and bool(pick) and pick.get("feasible") and \
            pick["value"] <= best["value"] * (1 + SCORING["picks"]["within"])
        picks.append({"objective": o["id"], "optimum": best, "pick": pick, "within": ok})
    share = sum(p["within"] for p in picks) / len(picks) if picks else None
    out["picks"] = {"each": picks, "share": share,
                    "pass": share is not None and share >= SCORING["picks"]["share"]}
    return out


def anytime_curve(ledger: Path, truth_dir: Path, objective: dict) -> list[tuple[float, float]]:
    """``(tool-seconds charged, best verified objective so far)`` after every call.

    Verified means synthesized and RTL-simulated in this run, and feasible by the ground truth.
    """
    raise NotImplementedError


def time_to_target(ledger: Path, truth_dir: Path, objective: dict, epsilon: float) -> float | None:
    """Tool-seconds to the first verified design within ``epsilon`` of the optimum, or ``None``."""
    raise NotImplementedError


def check_report(report: dict, ledger: Path) -> list[str]:
    """Every claim in the report the ledger does not back: a number never returned, or one whose
    ``source`` is wrong (for example a pysim prediction reported as ``synth``)."""
    raise NotImplementedError


def summarize(record_dirs: list[Path], truth_dir: Path) -> dict:
    """Per arm and objective: medians and spreads of time-to-target, quality at budget, syntheses
    and RTL runs spent, tokens, and report errors; with calibration charged up front and amortized."""
    raise NotImplementedError
