"""Read-only, bounded evidence projections for recovery."""
import json

from examples.dse_fir.contracts import identity
from examples.dse_fir.service import DseService
from examples.dse_fir.tests.test_service import POINT, Backend, quality, service


def test_checkpoint_identity_revision_and_read_only_restart(tmp_path):
    api = service(tmp_path)
    empty = api.context()["checkpoint"]
    assert empty["schema_version"] == "fir-checkpoint-v1"
    assert empty["policy_version"] == "evidence-checkpoint-v1"
    assert empty["revision"] == 0
    assert empty["best_feasible"] is None
    assert empty["results_cursor"] is None
    row = api.invoke("dse_pysim", {"params": POINT})
    context = api.context()
    checkpoint = context["checkpoint"]
    assert checkpoint["experiment_id"] == context["experiment_id"]
    assert checkpoint["revision"] == 1
    assert checkpoint["checkpoint_id"] != empty["checkpoint_id"]
    assert checkpoint["checkpoint_id"] == identity({k: v for k, v in checkpoint.items() if k != "checkpoint_id"})
    assert checkpoint["budget"] == context["budget"]
    assert checkpoint["constraints"] == context["experiment"]["constraints"]
    assert checkpoint["counts"] == {"observations": 1, "candidates": 1, "statuses": {"ok": 1}}
    assert checkpoint["recent"] == [{"evaluation_id": row["evaluation_id"], "candidate_id": row["candidate_id"],
                                      "operation": "pysim", "status": "ok", "evidence_kind": "fixed_point_python"}]
    assert checkpoint["omitted_observations"] == 0
    assert checkpoint["results_cursor"] == 0
    assert api.invoke("dse_pysim", {"params": POINT})["cached"]
    assert api.context()["checkpoint"] == checkpoint
    assert service(tmp_path).context()["checkpoint"] == checkpoint
    assert api.results()["total"] == 1


def test_best_is_compact_and_references_exact_evidence(tmp_path):
    api = service(tmp_path)
    api.invoke("dse_pysim", {"params": POINT})
    api.invoke("dse_predict_resource", {"params": POINT})
    assert api.context()["checkpoint"]["best_feasible"] is None
    api.invoke("dse_synth", {"params": [POINT]})
    api.invoke("dse_rtlsim", {"params": [POINT]})
    context = api.context()
    full = context["best_feasible"]
    best = context["checkpoint"]["best_feasible"]
    assert set(best) == {"candidate_id", "params", "quality", "resources", "evaluation_refs"}
    for key in ("candidate_id", "params", "quality", "resources"):
        assert best[key] == full[key]
    assert best["evaluation_refs"] == {
        op: {"evaluation_id": row["evaluation_id"], "status": row["status"],
             "evidence_kind": row["result"]["evidence_kind"]}
        for op, row in full["observations"].items()}


def test_adversarial_metadata_names_and_statuses_are_bounded(tmp_path):
    huge = "雪" * 10000

    class Noisy(Backend):
        def synth(self, params):
            result = super().synth(params)
            result["metrics"].update({huge: huge, "nested": {"huge": huge}, "other": 10**2000})
            result["provenance"] = {huge: huge}
            return result

        def rtlsim(self, params):
            return {"status": huge + str(params), "evidence_kind": huge, "metrics": {}}

    api = DseService(tmp_path, backend=Noisy(), evaluator=quality, evaluator_identity="test-quality-v1")
    api.invoke("dse_pysim", {"params": POINT})
    api.invoke("dse_synth", {"params": [POINT]})
    for i in range(12):
        api.invoke(huge + str(i), {})
        api.invoke("dse_rtlsim", {"params": [{"samp_i": i % 8 + 1, "ntap": 8 if i < 8 else 16}]})
    checkpoint = api.context()["checkpoint"]
    assert len(json.dumps(checkpoint).encode()) <= 8192
    assert checkpoint["best_feasible"]["resources"] == {"top_dsp": 32, "top_lut": 1200}
    assert len(checkpoint["recent"]) <= 5
    assert checkpoint["omitted_observations"] == checkpoint["revision"] - len(checkpoint["recent"])
    assert sum(checkpoint["counts"]["statuses"].values()) == checkpoint["revision"]
    assert checkpoint["counts"]["statuses"]["other"] == 2
    rows = {r["evaluation_id"]: r for r in api.results()["rows"]}
    for ref in checkpoint["recent"]:
        row = rows[ref["evaluation_id"]]
        for key in ("operation", "status", "evidence_kind"):
            original = row["result"][key] if key == "evidence_kind" else row[key]
            assert ref[key] == original or ref[key] == "sha256:" + identity(original)


def test_checkpoint_uses_context_snapshot_during_concurrent_write(tmp_path, monkeypatch):
    api = service(tmp_path)
    read_rows = api.store.rows
    calls = []

    def interleaved_read():
        calls.append(True)
        rows = read_rows()
        api.invoke("dse_pysim", {"params": POINT})
        return rows

    monkeypatch.setattr(api.store, "rows", interleaved_read)
    context = api.context()
    checkpoint = context["checkpoint"]
    assert len(calls) == 1
    assert checkpoint["revision"] == checkpoint["counts"]["candidates"] == 0
    assert checkpoint["budget"] == context["budget"]
    assert checkpoint["budget"]["spent"]["pysim"] == 0
    assert checkpoint["recent"] == []


def test_oversized_numeric_evidence_and_constraints_stay_bounded(tmp_path):
    huge = 10**3000

    def extreme_quality(params, evaluation):
        return {**quality(params, evaluation), "stopband_rej_db": huge,
                "metadata": {"arbitrary": "x" * 20000}}

    api = DseService(tmp_path, {"constraints": {"max_top_lut": huge}}, backend=Backend(),
                     evaluator=extreme_quality, evaluator_identity="extreme-v1")
    api.invoke("dse_pysim", {"params": POINT})
    api.invoke("dse_synth", {"params": [POINT]})
    checkpoint = api.context()["checkpoint"]
    assert len(json.dumps(checkpoint).encode()) <= 8192
    assert checkpoint["constraints"]["max_top_lut"] == {"omitted": True, "sha256": identity(huge)}
    assert "stopband_rej_db" not in checkpoint["best_feasible"]["quality"]
    assert "metadata" not in checkpoint["best_feasible"]["quality"]
    assert checkpoint["best_feasible"]["evaluation_refs"]["pysim"]["status"] == "ok"


def test_byte_guard_removes_oldest_recent_refs_and_rehashes(tmp_path, monkeypatch):
    from examples.dse_fir import checkpoint as projection

    api = service(tmp_path)
    for i in range(8):
        api.invoke("unknown_operation", {"i": i})
    original = api.context()["checkpoint"]
    assert len(original["recent"]) == 5
    assert original["omitted_observations"] == 3
    limit = len(json.dumps(original).encode()) - 100
    monkeypatch.setattr(projection, "MAX_BYTES", limit)
    bounded = api.context()["checkpoint"]
    assert len(json.dumps(bounded).encode()) <= limit
    assert len(bounded["recent"]) < 5
    assert bounded["recent"] == original["recent"][-len(bounded["recent"]):]
    assert bounded["omitted_observations"] == 8 - len(bounded["recent"])
    assert bounded["checkpoint_id"] == identity({k: v for k, v in bounded.items() if k != "checkpoint_id"})
