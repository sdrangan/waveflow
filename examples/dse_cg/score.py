"""score.py -- turn run records into the paper's numbers (plan ``plans/dse_cg.md``, *Measurements*).

Reads only ledgers, reports and the ground-truth table; never re-runs anything.

Three parts: the ground truth (:class:`Truth`, G1) and pysim's scoring against it (plan step 4); one
run's measures from its ledger (:func:`verified`, :func:`anytime_curve`, :func:`time_to_target`,
:func:`spend`, :func:`check_report`); and the summary over runs (:func:`summarize`, plan steps 6-7).

A run's record is a directory: ``record.json`` (``arm``, ``objective``, ``agent`` -- ``"scripted"``
or the model --, ``seed``, ``void`` and why, and for an agent its ``usage`` and ``wall_s``) and
``ledger.jsonl`` (the service's ledger, one entry a call).  ``policies.run_scripted`` and
``run_agent.py`` write them.
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


def workloads(objective: dict) -> tuple[list[dict], list[dict]]:
    """The objective's workloads, ``(timing, accuracy)``, each ``{"name", **parameters}``: those of its
    constraints (their ``at`` filled in) and of its minimized metric.  A timing workload is one a
    ``t_`` metric is stated on (it needs ``rtlsim``); the rest are accuracy (``golden``)."""
    items = [(c["metric"], c.get("workload"), c.get("at", {})) for c in objective.get("subject_to", [])]
    if objective.get("workload"):
        items.append((objective["minimize"], objective["workload"], {}))
    timing: list[dict] = []
    accuracy: list[dict] = []
    for metric, w, at in items:
        if w is None:
            continue
        w = ({"name": w} if isinstance(w, str) else dict(w)) | dict(at)
        out = timing if metric.startswith("t_") else accuracy
        if w not in out:
            out.append(w)
    return timing, accuracy


def load_ledger(path: Path) -> list[dict]:
    """A ledger file's entries (``service.LedgerEntry`` as dicts), in order."""
    import json

    return [json.loads(x) for x in Path(path).read_text(encoding="utf-8").splitlines() if x.strip()]


def _rows(e: dict) -> list[dict]:
    r = e.get("result")
    return [x for x in (r if isinstance(r, list) else [r]) if isinstance(x, dict)]


def _pkey(p: dict) -> str:
    import json

    return json.dumps(p, sort_keys=True)


def _wkey(design, w) -> str:
    """A workload as the service files it: its name and every parameter, defaults filled."""
    import json

    name, wf = design.workload(w)
    return json.dumps({"name": name, **wf}, sort_keys=True)


def verified(entries: list[dict], truth: Truth, objective: dict) -> list[dict]:
    """The designs a run verified, in the order it did.

    Verified is the submit rule: synthesized and RTL-simulated on every timing workload of the
    objective in this run -- an ``rtlsim`` row (``ok`` or ``mismatch``) on each, which the service
    returns only after the design's synthesis.  An objective with no timing workload needs a
    synthesis.  Each design once, at the entry that completed it: ``{"params", "seq", "spent_s"``
    (tool time charged through that entry), ``"clock_s"`` (the tool clock when its last job ended),
    ``"measured"``, ``"feasible"``, ``"value"``} -- the last three from the ground truth.
    """
    timing, _ = workloads(objective)
    need = {_wkey(truth.design, w) for w in timing}
    have: dict[str, set] = {}
    ends: dict[str, float] = {}
    done: set[str] = set()
    out, spent = [], 0.0
    for e in entries:
        spent += float(e.get("cost_s", 0.0))
        if e["fn"] not in ("synth", "rtlsim"):
            continue
        for row in _rows(e):
            p = row.get("params")
            ok = row.get("status") in (("ok",) if e["fn"] == "synth" else ("ok", "mismatch"))
            if not ok or not isinstance(p, dict):
                continue
            k = _pkey(p)
            ends[k] = max(ends.get(k, 0.0), float(e.get("t_end_s", 0.0)))
            if e["fn"] == "rtlsim":
                have.setdefault(k, set()).add(_wkey(truth.design, row.get("workload")))
            if k in done or not need <= have.get(k, set()):
                continue
            done.add(k)
            m = truth.measured(p) or {}
            ok = feasible(m, objective)
            out.append({"params": p, "seq": e["seq"], "spent_s": spent, "clock_s": ends[k],
                        "measured": m, "feasible": ok,
                        "value": objective_value(m, objective) if ok else None})
    return out


def anytime_curve(events: list[dict]) -> list[dict]:
    """The best verified objective so far, a step at each improvement: ``[{"spent_s", "clock_s",
    "value", "params"}]`` from :func:`verified`'s events.  Before the first step there is no
    verified feasible design."""
    curve: list[dict] = []
    for ev in events:
        if ev["feasible"] and ev["value"] is not None and (not curve or ev["value"] < curve[-1]["value"]):
            curve.append({k: ev[k] for k in ("spent_s", "clock_s", "value", "params")})
    return curve


def best_at(curve: list[dict], spent_s: float):
    """The best verified objective once *spent_s* tool-seconds were charged, or ``None``."""
    v = None
    for c in curve:
        if c["spent_s"] > spent_s:
            break
        v = c["value"]
    return v


def time_to_target(curve: list[dict], optimum_value: float, epsilon: float) -> dict | None:
    """The first verified design within *epsilon* of the optimum (value <= optimum * (1 + epsilon)):
    ``{"spent_s", "clock_s"}``, tool time charged and the tool clock with the run's workers -- or
    ``None`` if the run never reached it."""
    for c in curve:
        if c["value"] <= optimum_value * (1 + epsilon):
            return {"spent_s": c["spent_s"], "clock_s": c["clock_s"]}
    return None


def submitted(entries: list[dict]) -> dict | None:
    """The run's accepted submission, ``{"params", "report"}``, or ``None``."""
    for e in entries:
        if e["fn"] == "submit":
            r = e["result"]
            return {"params": r["params"], "report": r.get("report", {})}
    return None


def spend(entries: list[dict], truth: Truth, objective: dict) -> dict:
    """What a run spent: tool time by function; the parts it synthesized, the RTL builds it
    elaborated and the RTL runs it simulated (each charged once, as the service charges them); and
    the share of synthesis and RTL time spent on designs that fail a constraint (by the ground
    truth)."""
    by: dict[str, float] = {}
    parts: set[str] = set()
    builds: set[str] = set()
    runs: set[str] = set()
    wasted = verify = 0.0
    feas: dict[str, bool] = {}
    for e in entries:
        by[e["fn"]] = by.get(e["fn"], 0.0) + float(e.get("cost_s", 0.0))
        if e["fn"] not in ("synth", "rtlsim"):
            continue
        for row in _rows(e):
            p = row.get("params")
            if not isinstance(p, dict) or str(row.get("status", "")).startswith("illegal"):
                continue
            parts |= set(truth.identities(p).values())
            if e["fn"] == "rtlsim" and row.get("status") in ("ok", "mismatch"):
                sid = truth.design.sim_identity(p, row.get("workload"))
                builds.add(sid["build"])
                runs.add(sid["run"])
            k = _pkey(p)
            if k not in feas:
                feas[k] = feasible(truth.measured(p) or {}, objective)
            c = float(row.get("cost_s", 0.0))
            verify += c
            if not feas[k]:
                wasted += c
    return {"by_function_s": by, "total_s": sum(by.values()), "syntheses": len(parts),
            "rtl_builds": len(builds), "rtl_runs": len(runs),
            "wasted_share": wasted / verify if verify else 0.0}


def _close(claimed, actual) -> bool:
    """A reported number backs a returned one: equal within 0.5% or within its own rounding."""
    try:
        c, a = float(claimed), float(actual)
    except (TypeError, ValueError):
        return False
    txt = repr(claimed)
    decimals = len(txt.split(".")[1]) if "." in txt and "e" not in txt else 0
    return abs(c - a) <= max(0.005 * abs(a), 0.5 * 10.0 ** -decimals, 1e-12)


def _values(row: dict, metric: str) -> list:
    return [v for v in (row.get(metric), (row.get("resources") or {}).get(metric)) if v is not None]


def check_report(report: dict, entries: list[dict], design, objective: dict | None = None) -> list[str]:
    """Every claim in the report the ledger does not back.

    Each ``evidence`` entry (``metric``, ``value``, ``source``, optional ``workload``) is backed when
    a row of the submitted design that ``source`` returned in this run holds that value (within 0.5%
    or the number's own rounding), on that workload if one is named.  The errors: a number no call
    returned for the design; a number another function returned (``source`` wrong, for example a
    pysim prediction reported as ``rtlsim``); a report naming another design than the one submitted;
    and, given the *objective*, a constraint or the minimized metric with no evidence at all."""
    sub = submitted(entries)
    errors: list[str] = []
    if sub is None:
        return ["no accepted submission"]
    p = sub["params"]
    if report.get("params") is not None and _pkey(design.fill(report["params"])) != _pkey(p):
        errors.append(f"the report's params {report['params']} are not the design submitted")
    rows: list[tuple[str, str | None, dict]] = []           # (function, workload key, row)
    for e in entries:
        if e["fn"] not in ("golden", "pysim", "synth", "rtlsim", "hls_estimate"):
            continue
        for row in _rows(e):
            if not isinstance(row.get("params"), dict) or _pkey(row["params"]) != _pkey(p):
                continue
            if e["fn"] in ("golden", "pysim"):
                w = (e.get("args") or {}).get("workload")
                wk = _wkey(design, "accuracy" if w is None and e["fn"] == "golden" else w)
            elif e["fn"] == "rtlsim":
                wk = _wkey(design, row.get("workload"))
            else:
                wk = None
            rows.append((e["fn"], wk, row))
    for ev in report.get("evidence", []):
        metric, value, source = ev.get("metric"), ev.get("value"), ev.get("source")
        wk = None
        if ev.get("workload") is not None:
            try:
                wk = _wkey(design, ev["workload"])
            except (KeyError, ValueError, TypeError):
                errors.append(f"{metric}={value}: unknown workload {ev['workload']}")
                continue
        cands = [(fn, row) for fn, w, row in rows if wk is None or w is None or w == wk]
        if any(fn == source and any(_close(value, v) for v in _values(row, metric)) for fn, row in cands):
            continue
        other = sorted({fn for fn, row in cands if any(_close(value, v) for v in _values(row, metric))})
        if other:
            errors.append(f"{metric}={value}: returned by {', '.join(other)}, not {source}")
        else:
            errors.append(f"{metric}={value} ({source}): never returned for the submitted design")
    if objective is not None:
        named = {ev.get("metric") for ev in report.get("evidence", [])}
        wanted = [c["metric"] for c in objective.get("subject_to", [])] + [objective["minimize"]]
        errors += [f"no evidence for {m}" for m in dict.fromkeys(wanted) if m not in named]
    return errors


def run_measures(record_dir: Path, truth: Truth, objective: dict, optimum_value: float | None,
                 epsilon: float) -> dict:
    """One run's measures from its record (see the module's docstring)."""
    import json

    rec = json.loads((Path(record_dir) / "record.json").read_text(encoding="utf-8"))
    entries = load_ledger(Path(record_dir) / "ledger.jsonl")
    events = verified(entries, truth, objective)
    curve = anytime_curve(events)
    sub = submitted(entries)
    final = curve[-1]["value"] if curve else None
    out = {"record": str(record_dir), "arm": rec["arm"], "objective": rec["objective"],
           "agent": rec.get("agent"), "seed": rec.get("seed"), "void": bool(rec.get("void")),
           "curve": curve, "verified": len(events), "best": final,
           "time_to_target": None if optimum_value is None else time_to_target(curve, optimum_value, epsilon),
           "quality": None if final is None or not optimum_value else final / optimum_value - 1,
           "spend": spend(entries, truth, objective), "usage": rec.get("usage"), "wall_s": rec.get("wall_s")}
    if sub is not None:
        m = truth.measured(sub["params"]) or {}
        ok = feasible(m, objective)
        v = objective_value(m, objective) if ok else None
        out["submitted"] = {"params": sub["params"], "feasible": ok, "value": v,
                            "quality": None if v is None or not optimum_value else v / optimum_value - 1}
        out["report_errors"] = check_report(sub["report"], entries, truth.design, objective)
    else:
        out["submitted"], out["report_errors"] = None, None
    return out


def _stats(xs: list) -> dict:
    """Median, quartiles and range of *xs*; ``None`` (never reached) sorts as infinity."""
    import math

    import numpy as np

    n = len(xs)
    if not n:
        return {"n": 0}
    v = np.array([math.inf if x is None else float(x) for x in xs])

    def q(f):
        x = float(np.quantile(v, f, method="lower"))
        return None if math.isinf(x) else x

    return {"n": n, "reached": int(np.isfinite(v).sum()), "median": q(0.5), "q25": q(0.25),
            "q75": q(0.75), "min": q(0.0), "max": q(1.0)}


def summarize(record_dirs: list[Path], truth: Truth, objectives: list[dict], designs: list[dict],
              epsilon: float, calibration_s: float | None = None) -> dict:
    """Per arm and objective, over the runs that are not void: time to target (tool time charged and
    the tool clock), quality at the budget (the best verified design, and the submitted one, against
    the optimum), syntheses, RTL builds and runs, the share spent on infeasible designs, tokens and
    wall time, and report errors.

    *calibration_s*, the Waveflow arm's calibration (the units' builds and the integration builds,
    plan *Fairness rules* 2), enters its time to target two ways: ``upfront`` (added to every run, the
    headline) and ``amortized`` (divided over the Waveflow runs summarized)."""
    import json

    by_id = {o["id"]: o for o in objectives}
    opt = {o["id"]: optimum(truth, o, designs) for o in objectives}
    runs = []
    for d in record_dirs:
        rec = json.loads((Path(d) / "record.json").read_text(encoding="utf-8"))
        o = by_id[rec["objective"]]
        runs.append(run_measures(Path(d), truth, o, opt[o["id"]].get("value"), epsilon))
    n_wf = sum(1 for r in runs if r["arm"] == "waveflow" and not r["void"])
    out: dict = {"optimum": opt, "epsilon": epsilon, "calibration_s": calibration_s, "groups": {}}
    for key in sorted({(r["arm"], r["objective"]) for r in runs}):
        rs = [r for r in runs if (r["arm"], r["objective"]) == key and not r["void"]]
        ttt = [(r["time_to_target"] or {}).get("spent_s") for r in rs]
        g: dict = {"runs": len(rs), "void": sum(1 for r in runs if (r["arm"], r["objective"]) == key and r["void"]),
                   "time_to_target_s": _stats(ttt),
                   "time_to_target_clock_s": _stats([(r["time_to_target"] or {}).get("clock_s") for r in rs]),
                   "quality": _stats([r["quality"] for r in rs]),
                   "submitted_feasible": sum(1 for r in rs if r["submitted"] and r["submitted"]["feasible"]),
                   "submitted_quality": _stats([(r["submitted"] or {}).get("quality") for r in rs]),
                   "syntheses": _stats([r["spend"]["syntheses"] for r in rs]),
                   "rtl_runs": _stats([r["spend"]["rtl_runs"] for r in rs]),
                   "spent_s": _stats([r["spend"]["total_s"] for r in rs]),
                   "wasted_share": _stats([r["spend"]["wasted_share"] for r in rs]),
                   "report_errors": _stats([None if r["report_errors"] is None else len(r["report_errors"])
                                            for r in rs]),
                   "output_tokens": _stats([(r["usage"] or {}).get("output_tokens") for r in rs if r["usage"]]),
                   "wall_s": _stats([r["wall_s"] for r in rs if r["wall_s"] is not None])}
        if key[0] == "waveflow" and calibration_s:
            g["time_to_target_s_upfront"] = _stats([None if x is None else x + calibration_s for x in ttt])
            g["time_to_target_s_amortized"] = _stats([None if x is None else x + calibration_s / max(n_wf, 1)
                                                      for x in ttt])
        out["groups"]["/".join(key)] = g
    out["runs"] = runs
    return out
