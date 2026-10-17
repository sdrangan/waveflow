"""Real subprocess recovery; no inference or hardware tools."""
import importlib.util
import json

import pytest


def test_recovery_probe_preserves_evidence_and_charges(tmp_path):
    assert importlib.util.find_spec("examples.dse_fir.recovery_probe") is not None
    from examples.dse_fir.recovery_probe import run_probe

    root = tmp_path / "probe"
    result = run_probe(root)
    assert result["passed"] is True
    assert result["model_calls"] == 0
    assert set(result["arms"]) == {"pull", "resume"}
    assert result["checkpoint_parity"] is True
    for policy, arm in result["arms"].items():
        assert all(arm["checks"].values())
        assert arm["budget"]["spent"]["synth"] == 1
        assert arm["selection_score"]["valid"] is True
        assert arm["selection_score"]["selection_gap_db"] == 0
        record = json.loads((root / policy / "exposure.json").read_text())
        assert record["policy"] == policy
        assert record["event"] == "scripted_context_delivery"
        assert record["checkpoint"]["checkpoint_id"] == arm["checkpoint_id"]
    with pytest.raises(FileExistsError):
        run_probe(root)
