"""Host-neutral FIR application boundary: six operations, one evidence contract."""
from __future__ import annotations

import hashlib
import math
from importlib.metadata import version
from pathlib import Path
from typing import Protocol

from pydantic import ValidationError

from .contracts import Candidate, Experiment, canonical
from .dse_store import Store

OPERATIONS = ("pysim", "predict_resource", "synth", "rtlsim")


class Backend(Protocol):
    """Bounded, side-effect-free backend; not a generic arbitrary-code plugin."""
    identity: dict

    def capabilities(self) -> dict: ...
    def synth(self, params: dict) -> dict: ...
    def rtlsim(self, params: dict) -> dict: ...
    def predict_resource(self, params: dict) -> dict: ...


def _number(value):
    return type(value) is int or (type(value) is float and math.isfinite(value))


def _source_identity():
    here = Path(__file__).parent
    sources = ("contracts.py", "fir_quality.py", "fir_metrics.py", "service.py", "dse_store.py")
    return {"files": {name: hashlib.sha256((here / name).read_bytes()).hexdigest()
                      for name in sources if (here / name).is_file()},
            "dependencies": {name: version(name) for name in ("numpy", "scipy", "pydantic")}}


class DseService:
    def __init__(self, root, config=None, *, backend: Backend | None = None, evaluator=None,
                 evaluator_identity: str | None = None):
        if backend is None:
            from .backends import ReplayBackend
            backend = ReplayBackend()
        if evaluator is None:
            from .fir_metrics import evaluate
            evaluator = evaluate
            evaluator_identity = "builtin-fir-quality"
        elif not isinstance(evaluator_identity, str) or not evaluator_identity.strip():
            raise ValueError("custom evaluator requires a versioned evaluator_identity")
        self.backend = backend
        self.evaluator = evaluator
        normalized = None if config is None else Experiment.model_validate(config).model_dump()
        self.store = Store(root, normalized, {"version": "fir-experiment-v1",
                           "source": _source_identity(), "backend": backend.identity,
                           "evaluator": evaluator_identity})
        self.config = Experiment.model_validate(self.store.manifest["config"])

    def _invalid(self, operation, arguments, message):
        try:
            canonical(arguments)
        except (TypeError, ValueError, OverflowError, RecursionError):
            # Non-JSON Python callers and NaN/Inf must not break the error path.
            arguments = {"invalid_arguments_repr": repr(arguments)}
        return self.store.run(operation, arguments, arguments, 0, dict, invalid=message)

    def invoke(self, name, arguments):
        """Identical validation for Python, CLI, MCP, and client-managed tool loops."""
        if not isinstance(name, str):
            return self._invalid(str(name), {"arguments": arguments}, "operation must be a string")
        if not isinstance(arguments, dict):
            return self._invalid(str(name), {"arguments": arguments}, "arguments must be an object")
        operation = name.removeprefix("dse_")
        if operation == "get_dse_context" and not arguments:
            return self.context()
        if operation == "get_results":
            if set(arguments) - {"offset", "limit"}:
                return self._invalid(operation, arguments, "unknown result query fields")
            offset, limit = arguments.get("offset", 0), arguments.get("limit", 100)
            if type(offset) is not int or type(limit) is not int or offset < 0 or not 1 <= limit <= 1000:
                return self._invalid(operation, arguments, "require offset >= 0 and 1 <= limit <= 1000")
            return self.results(offset, limit)
        if operation not in OPERATIONS or set(arguments) != {"params"}:
            return self._invalid(operation, arguments, "unknown operation or fields; supply only params")
        params = arguments["params"]
        if operation in ("synth", "rtlsim"):
            if not isinstance(params, list) or not 1 <= len(params) <= 64:
                return self._invalid(operation, arguments, "params must be a nonempty batch of at most 64 points; none executed")
            rows = [self._evaluate(operation, p) for p in params]
            return {"rows": rows, "requested_count": len(params), "returned_count": len(rows)}
        return self._evaluate(operation, params)

    def _evaluate(self, operation, params):
        try:
            point = Candidate.model_validate(params).model_dump()
        except ValidationError as exc:
            return self._invalid(operation, {"params": params}, str(exc))

        def compute():
            if operation == "pysim":
                metrics = self.evaluator(point, self.config.evaluation.model_dump())
                return {"status": "ok", "evidence_kind": "fixed_point_python",
                        "metrics": metrics, "provenance": {"evaluation": self.config.evaluation.model_dump()}}
            return getattr(self.backend, operation)(point)

        return self.store.run(operation, point, point, getattr(self.config.budget, operation), compute)

    def context(self):
        observations = self.store.rows()
        rows = self._join(observations)
        feasible = [r for r in rows if r["feasible"] is True]
        best = max(feasible, key=lambda r: r["quality"]["stopband_rej_db"], default=None)
        # Budget and evidence describe one SQLite read, even with concurrent writers.
        spent = {op: sum(r["cost"]["units"] for r in observations if r["operation"] == op)
                 for op in OPERATIONS}
        budget = self.config.budget.model_dump()
        return {"schema_version": "fir-experiment-v1", "experiment_id": self.store.manifest["experiment_id"],
                "experiment": self.config.model_dump(), "parameter_schema": Candidate.model_json_schema(),
                "capabilities": self.backend.capabilities(),
                "costs": {op: {"units_per_uncached_point": 1, "cache_hit_units": 0,
                               "unit": "replay_query" if op == "synth" else "evaluation",
                               "estimated_live_seconds_not_charged": {"synth": 50, "rtlsim": 90}.get(op)}
                          for op in OPERATIONS},
                "budget": {"limits": budget, "spent": {op: spent.get(op, 0) for op in OPERATIONS},
                           "remaining": {op: budget[op] - spent.get(op, 0) for op in OPERATIONS}},
                "best_feasible": best, "candidate_count": len(rows),
                "semantics": {"quality": "gain-normalized periodic fixed-point waveform metric; not a worst-bin guarantee",
                              "throughput": "closed-form kernel rate assuming II=1; not board throughput",
                              "feasible": "quality + replayed HLS resource constraints only; not RTL/physical validation",
                              "batch": "one result per requested point, including invalid, missing and budget failures",
                              "scope": "fixed FIR architecture; no authoring, shell, toolchain or board authority",
                              "restart": "same root resumes evidence and budget; changed configuration/code needs new root"}}

    def _join(self, observations):
        joined = {}
        for row in observations:
            cid = row["candidate_id"]
            if cid is None:
                continue
            item = joined.setdefault(cid, {"candidate_id": cid, "params": row["params"], "observations": {}})
            item["observations"][row["operation"]] = row
        for item in joined.values():
            obs = item["observations"]
            quality = obs.get("pysim", {})
            synth = obs.get("synth", {})
            prediction = obs.get("predict_resource", {})
            item["quality"] = quality.get("result", {}).get("metrics") if quality.get("status") == "ok" else None
            item["resources"] = synth.get("result", {}).get("metrics") if synth.get("status") == "ok" else None
            item["predictions"] = prediction.get("result", {}).get("metrics") if prediction.get("status") == "ok" else None
            q, r, c = item["quality"], item["resources"], self.config.constraints
            required_q = ("passband_sndr_db", "throughput_samp_per_cyc", "stopband_rej_db")
            complete = (q is not None and r is not None
                        and synth.get("result", {}).get("evidence_kind") in {"hls_estimate_replay", "hls_estimate"}
                        and all(_number(q.get(k)) for k in required_q)
                        and all(_number(r.get(k)) and r[k] >= 0 for k in ("top_dsp", "top_lut")))
            item["feasible"] = (q["passband_sndr_db"] >= c.min_passband_sndr_db and
                                q["throughput_samp_per_cyc"] >= c.min_throughput and
                                r["top_dsp"] <= c.max_top_dsp and r["top_lut"] <= c.max_top_lut) if complete else None
            item["discrepancies"] = {}
            if r and item["predictions"]:
                for key, pred in item["predictions"].items():
                    estimate = pred.get("est") if isinstance(pred, dict) else None
                    if _number(estimate) and _number(r.get(key)):
                        try:
                            delta = r[key] - estimate
                        except OverflowError:
                            continue
                        if _number(delta):
                            item["discrepancies"][key] = {"predicted": estimate, "replayed": r[key], "delta": delta}
        return list(joined.values())

    def results(self, offset=0, limit=100):
        observations = self.store.rows()
        candidates = self._join(observations)
        page = observations[offset:offset + limit]
        return {"experiment_id": self.store.manifest["experiment_id"], "rows": page,
                "total": len(observations), "offset": offset, "returned_count": len(page),
                "has_more": offset + len(page) < len(observations),
                "next_offset": offset + len(page) if offset + len(page) < len(observations) else None,
                "candidates": candidates, "candidate_count": len(candidates)}
