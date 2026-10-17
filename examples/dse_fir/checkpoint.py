"""Deterministic read-only projection; never an evaluation/cache identity policy.

The projection version belongs to the checkpoint, not Store's evaluation key.
Service source hashing already freezes service upgrades for existing roots.
"""
from __future__ import annotations

import json
import math
from collections import Counter

from .contracts import canonical, identity

SCHEMA_VERSION = "fir-checkpoint-v1"
POLICY_VERSION = "evidence-checkpoint-v1"
MAX_BYTES = 8192
_OPERATIONS = ("pysim", "predict_resource", "synth", "rtlsim")
_STATUSES = frozenset(("ok", "invalid", "failed", "unavailable", "missing", "budget_exhausted"))
_QUALITY = ("stopband_rej_db", "passband_sndr_db", "throughput_samp_per_cyc")
_RESOURCES = ("top_dsp", "top_lut")


def _label(value):
    # Hash rather than truncate: never misrepresent two long labels as identical.
    return value if len(canonical(value).encode()) <= 96 else "sha256:" + identity(value)


def _ref(row):
    return {"evaluation_id": row["evaluation_id"], "status": _label(row["status"]),
            "evidence_kind": _label(row["result"]["evidence_kind"])}


def _metrics(values, keys):
    # Whitelisting excludes backend metadata, arrays, and arbitrary extra keys.
    return {key: value for key in keys if key in values
            and (type(value := values[key]) is int or
                 (type(value) is float and math.isfinite(value)))
            and len(canonical(value)) <= 64}


def project_checkpoint(*, experiment_id, observations, candidate_count, best, budget, constraints):
    """Project one already-read snapshot; no storage reads, writes, or charging.

    Bound default JSON UTF-8 (also bounds canonical JSON). Unknown status counts
    aggregate under ``other``; oversized labels/constraints use canonical hashes.
    Metrics are finite numeric whitelisted scalars, at most 64 characters each.
    """
    recent = [{**_ref(row), "candidate_id": row["candidate_id"],
               "operation": _label(row["operation"])} for row in observations[-5:]]
    if best is not None:
        best = {"candidate_id": best["candidate_id"], "params": best["params"],
                "quality": _metrics(best["quality"], _QUALITY),
                "resources": _metrics(best["resources"], _RESOURCES),
                "evaluation_refs": {op: _ref(best["observations"][op]) for op in _OPERATIONS
                                    if op in best["observations"]}}
    checkpoint = {
        "schema_version": SCHEMA_VERSION, "policy_version": POLICY_VERSION,
        "experiment_id": experiment_id, "revision": len(observations),
        "budget": budget,
        "constraints": {key: value if len(canonical(value)) <= 64 else
                        {"omitted": True, "sha256": identity(value)} for key, value in constraints.items()},
        "best_feasible": best,
        "counts": {"observations": len(observations), "candidates": candidate_count,
                   "statuses": dict(Counter(row["status"] if row["status"] in _STATUSES else "other"
                                            for row in observations))},
        "recent": recent, "omitted_observations": len(observations) - len(recent),
        "results_cursor": 0 if observations else None,
        "semantics": "Predictions are not replayed HLS evidence. Feasible means quality + HLS resources, "
                     "not RTL/physical validation. Metrics are bounded numeric summaries; oversized labels "
                     "and constraints use SHA-256, unknown status counts use other. Full evidence: get_results.",
    }
    # Include the digest's serialized overhead in the hard output limit. If the
    # schema grows, shed recent refs first, never silently truncate identifiers.
    while True:
        checkpoint["checkpoint_id"] = identity({k: v for k, v in checkpoint.items() if k != "checkpoint_id"})
        if len(json.dumps(checkpoint, allow_nan=False).encode("utf-8")) <= MAX_BYTES:
            return checkpoint
        if not recent:
            raise ValueError("checkpoint core exceeds byte limit")
        recent.pop(0)
        checkpoint["omitted_observations"] += 1
