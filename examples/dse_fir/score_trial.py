"""Offline verdict for a real Hermes trial; no model calls and no reliance on its claims."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path

from .benchmark import score_experiment
from .contracts import Candidate, identity
from .run_agent import TOOL_NAMES
from .service import DseService


def read_events(path):
    events = []
    for block in Path(path).read_text().replace("\r\n", "\n").split("\n\n"):
        lines = block.splitlines()
        name = next((s[6:].strip() for s in lines if s.startswith("event:")), "")
        data = "\n".join(s[5:].lstrip() for s in lines if s.startswith("data:"))
        if data:
            events.append({"event": name, "data": json.loads(data)})
    return events


def recommendation(text):
    decoder = json.JSONDecoder()
    found = []
    for pos, char in enumerate(text):
        if char == "{":
            try:
                value, _ = decoder.raw_decode(text[pos:])
            except ValueError:
                continue
            if isinstance(value, dict) and "recommended_candidate_id" in value:
                found.append(value)
    return found[-1] if found else None


def check_recommendation(claim, candidates):
    """Accept only a feasible, correctly identified candidate with cited successful evidence."""
    checks = {"recommendation_parsed": claim is not None}
    if claim is None:
        return checks
    candidate = next((c for c in candidates if c["candidate_id"] == claim.get("recommended_candidate_id")), None)
    checks["candidate_exists"] = candidate is not None
    if candidate is None:
        return checks
    try:
        checks["params_match"] = identity(Candidate.model_validate(claim.get("params")).model_dump()) == candidate["candidate_id"]
    except ValueError:
        checks["params_match"] = False
    checks["feasibility_supported"] = candidate["feasible"] is True and claim.get("feasible") is True
    ids = claim.get("evaluation_ids", [])
    ids = list(ids.values()) if isinstance(ids, dict) else ids
    checks["evidence_cited"] = isinstance(ids, list) and all(
        candidate["observations"].get(op, {}).get("status") == "ok"
        and candidate["observations"][op]["evaluation_id"] in ids for op in ("pysim", "synth"))
    cited = [r for r in candidate["observations"].values() if r["evaluation_id"] in ids] if isinstance(ids, list) else []
    checks["citations_valid"] = isinstance(ids, list) and bool(ids) and len(cited) == len(ids) and all(r["status"] == "ok" for r in cited)
    value = claim.get("objective_value")
    if isinstance(value, dict):
        value = value.get("stopband_rej_db")
    actual = (candidate.get("quality") or {}).get("stopband_rej_db")
    checks["objective_matches"] = type(value) in (int, float) and actual is not None and math.isclose(value, actual, abs_tol=0.001)
    return checks


def score_trial(root, trace, reference=None):
    trace = Path(trace)
    raw = trace / "raw.sse"
    events = read_events(raw)
    terminals = [e for e in events if e["event"] in ("run.completed", "run.failed", "run.cancelled")]
    terminal = terminals[-1]["data"] if terminals else {}
    runtime = terminal.get("runtime", {})
    final = next((e["data"].get("content", "") for e in reversed(events) if e["event"] == "assistant.completed"), "")
    if not final:
        final = next((m.get("content", "") for m in reversed(terminal.get("messages", [])) if m.get("role") == "assistant"), "")
    api = DseService(root)
    results = api.results()
    claim = recommendation(final)
    calls = [e["data"].get("tool_name") for e in events if e["event"] == "tool.started"]
    allowed = {"mcp__waveflow__" + n for n in TOOL_NAMES}
    checks = {"completed": len(terminals) == 1 and terminals[0]["event"] == "run.completed" and terminal.get("completed") is True,
              "runtime_locked": runtime.get("provider") == "deepseek" and runtime.get("model") == "deepseek-flash" and runtime.get("model_lock") == "confirmed",
              "tool_boundary": bool(calls) and set(calls) <= allowed,
              "context_first": bool(calls) and calls[0] == "mcp__waveflow__dse_get_dse_context",
              **check_recommendation(claim, results["candidates"])}
    result = {"schema_version": "fir-trial-score-v1", "passed": all(checks.values()), "checks": checks,
              "runtime": runtime, "turn_exit_reason": terminal.get("turn_exit_reason"),
              "tool_call_count": len(calls), "usage": terminal.get("usage"),
              "budget": api.context()["budget"], "recommendation": claim,
              "experiment_id": api.store.manifest["experiment_id"],
              "raw_sse_sha256": hashlib.sha256(raw.read_bytes()).hexdigest(),
              "spillover_occurrences": raw.read_text().count("<persisted-output>"),
              "limitations": ["Single-trial functional test, not a model ranking or statistical estimate.",
                              "Verdict checks structured recommendation; narrative claims require review."]}
    if reference is not None:
        result["search_score"] = score_experiment(reference, root)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True)
    parser.add_argument("--trace", required=True)
    parser.add_argument("--reference")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    reference = json.loads(Path(args.reference).read_text()) if args.reference else None
    result = score_trial(args.root, args.trace, reference)
    Path(args.output).write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
