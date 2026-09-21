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
