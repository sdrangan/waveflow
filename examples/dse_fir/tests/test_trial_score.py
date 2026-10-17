"""Offline recommendation checks; paid model calls are never part of pytest."""
import copy
import json
from pathlib import Path

from examples.dse_fir.score_trial import check_recommendation, recommendation


def test_real_trial_verdicts_and_unsupported_claims():
    evidence = json.loads((Path(__file__).parents[1] / "evidence/first_trials.json").read_text())
    before, after = evidence["trials"]
    assert not all(check_recommendation(before["score"]["recommendation"], before["candidates"]).values())
    claim = after["score"]["recommendation"]
    assert all(check_recommendation(claim, after["candidates"]).values())
    assert recommendation("prefix\n```json\n" + json.dumps(claim) + "\n``` suffix") == claim
    bad = copy.deepcopy(claim)
    bad["evaluation_ids"]["synth"] = "fabricated-id"
    assert check_recommendation(bad, after["candidates"])["evidence_cited"] is False
    bad = copy.deepcopy(claim)
    bad["params"]["ntap"] = 16
    assert check_recommendation(bad, after["candidates"])["params_match"] is False
    assert not all(check_recommendation(None, after["candidates"]).values())


def test_selection_score_does_not_substitute_best_observed_for_recommendation(tmp_path):
    from examples.dse_fir import score_trial as scorer
    from examples.dse_fir.service import DseService

    assert hasattr(scorer, "score_selection")
    service = DseService(tmp_path)
    for taps in (16, 32):
        point = {"ntap": taps, "samp_w": 16}
        service.invoke("dse_pysim", {"params": point})
        service.invoke("dse_synth", {"params": [point]})
    candidates = service.results()["candidates"]
    feasible = [c for c in candidates if c["feasible"] is True]
    chosen = min(feasible, key=lambda c: c["quality"]["stopband_rej_db"])
    best = max(feasible, key=lambda c: c["quality"]["stopband_rej_db"])
    assert chosen["quality"]["stopband_rej_db"] < best["quality"]["stopband_rej_db"]
    claim = {"recommended_candidate_id": chosen["candidate_id"], "params": chosen["params"],
             "feasible": True, "objective_value": chosen["quality"]["stopband_rej_db"],
             "evaluation_ids": {op: chosen["observations"][op]["evaluation_id"] for op in ("pysim", "synth")}}
    result = scorer.score_selection(claim, candidates, best["quality"]["stopband_rej_db"])
    assert result["valid"] is True
    assert result["recommended_quality_db"] == chosen["quality"]["stopband_rej_db"]
    assert result["selection_gap_db"] > 0
    assert result["recommended_regret_db"] == result["selection_gap_db"]
    claim["evaluation_ids"]["synth"] = "fabricated"
    invalid = scorer.score_selection(claim, candidates, best["quality"]["stopband_rej_db"])
    assert invalid["valid"] is False
    assert invalid["recommended_quality_db"] is None
    assert invalid["recommended_regret_db"] is None
