"""Deterministic lost-response recovery probe; no model or hardware execution.

Run: python -m examples.dse_fir.recovery_probe --root /new/probe-root
The two scripted arms test checkpoint parity, not agent performance or Pi hooks.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

from .contracts import canonical, identity
from .score_trial import score_selection
from .service import DseService


def _arm(root, policy):
    root.mkdir()
    store_root = root / "experiment"
    api = DseService(store_root)
    point = {"ntap": 32, "samp_w": 16}
    quality = api.invoke("dse_pysim", {"params": point})
    # Commit, then discard the response. No conversational evidence crosses restart.
    api.invoke("dse_synth", {"params": [point]})
    before = api.context()["checkpoint"]
    del api

    started = time.monotonic()
    process = subprocess.run(
        [sys.executable, "-m", "examples.dse_fir", "--root", str(store_root), "context"],
        capture_output=True, text=True, check=True, timeout=120)
    elapsed = time.monotonic() - started
    context = json.loads(process.stdout)
    text = context["checkpoint_json"]
    checkpoint = json.loads(text)
    exposure = {"schema_version": "fir-recovery-exposure-v1", "policy": policy,
                "event": "scripted_context_delivery",
                "trigger": "explicit_context_request" if policy == "pull" else "session_resume",
                "checkpoint": checkpoint, "content": text,
                "content_sha256": identity(checkpoint), "bytes": len(text.encode()),
                "retrieval_seconds": elapsed, "provider_receipt": False}
    (root / "exposure.json").write_text(canonical(exposure) + "\n")

    resumed = DseService(store_root)
    selected = checkpoint["best_feasible"]
    claim = None if selected is None else {
        "recommended_candidate_id": selected["candidate_id"], "params": selected["params"],
        "feasible": True, "objective_value": selected["quality"]["stopband_rej_db"],
        "evaluation_ids": {op: selected["evaluation_refs"][op]["evaluation_id"]
                           for op in ("pysim", "synth")}}
    selection = score_selection(claim, resumed.results()["candidates"])
    # Explicit probe assertion, not an agent action: committed work cannot spend twice.
    retry = resumed.invoke("dse_synth", {"params": [point]})["rows"][0]
    after = resumed.context()["checkpoint"]
    checks = {"quality_succeeded": quality["status"] == "ok",
              "checkpoint_preserved": checkpoint == before,
              "binding_preserved": checkpoint["experiment_id"] == resumed.store.manifest["experiment_id"],
              "evidence_recovered": selected is not None and selection["valid"],
              "retry_cached": retry["cached"] is True and retry["cost"]["units"] == 0,
              "read_and_retry_uncharged": before["budget"] == after["budget"],
              "revision_preserved": before["revision"] == after["revision"],
              "bounded": len(text.encode()) <= 8192}
    return {"checks": checks, "checkpoint_id": checkpoint["checkpoint_id"],
            "budget": after["budget"], "selection_score": selection,
            "context_bytes": len(text.encode()), "retrieval_seconds": elapsed,
            "exposure_file": str(root / "exposure.json")}


def run_probe(root):
    """Exercise identical service state in fresh pull/resume roots; never overwrite."""
    root = Path(root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=False)
    arms = {policy: _arm(root / policy, policy) for policy in ("pull", "resume")}
    parity = arms["pull"]["checkpoint_id"] == arms["resume"]["checkpoint_id"]
    report = {"schema_version": "fir-recovery-probe-v1", "model_calls": 0,
              "checkpoint_parity": parity,
              "passed": parity and all(all(a["checks"].values()) for a in arms.values()),
              "arms": arms,
              "limitations": ["Scripted delivery; not a model comparison or Pi lifecycle test.",
                              "Lost response after local commit; no live-job cancellation claim.",
                              "Real Python evaluation and recorded HLS replay; no hardware run."]}
    (root / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, help="New output directory")
    args = parser.parse_args()
    report = run_probe(args.root)
    print(json.dumps(report, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
