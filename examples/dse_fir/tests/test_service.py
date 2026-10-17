"""Contract gates: persistence, budget accounting, and evidence separation."""
import json
from concurrent.futures import ThreadPoolExecutor
from typing import ClassVar

import pytest

from examples.dse_fir.contracts import Candidate
from examples.dse_fir.service import DseService


class Backend:
    identity: ClassVar[dict] = {"backend": "test-double-v1"}

    def capabilities(self):
        return {"synth": "test-only", "rtlsim": "unavailable"}

    def synth(self, params):
        return {"status": "ok", "evidence_kind": "hls_estimate_replay",
                "metrics": {"top_dsp": 32, "top_lut": 1200}, "provenance": {"fixture": True}}

    def predict_resource(self, params):
        return {"status": "ok", "evidence_kind": "prediction",
                "metrics": {"top_dsp": {"est": 30, "interval": None}}}

    def rtlsim(self, params):
        return {"status": "unavailable", "evidence_kind": "none", "metrics": {}}


def quality(params, evaluation):
    return {"stopband_rej_db": 40., "passband_sndr_db": 30.,
            "throughput_samp_per_cyc": 2.}


def service(root, config=None):
    return DseService(root, config, backend=Backend(), evaluator=quality, evaluator_identity="test-quality-v1")


def test_custom_evaluator_requires_frozen_identity(tmp_path):
    with pytest.raises(ValueError, match="evaluator_identity"):
        DseService(tmp_path, backend=Backend(), evaluator=quality)
    api = DseService(tmp_path, backend=Backend(), evaluator=quality, evaluator_identity="quality-v1")
    api.invoke("dse_pysim", {"params": {}})
    with pytest.raises(ValueError):
        DseService(tmp_path, backend=Backend(), evaluator=quality, evaluator_identity="quality-v2")


def test_prediction_returned_by_synth_cannot_prove_feasibility(tmp_path):
    class WrongKind(Backend):
        def synth(self, params):
            result = super().synth(params)
            result["evidence_kind"] = "resource_model_prediction"
            return result

    api = DseService(tmp_path, backend=WrongKind())
    api.invoke("dse_pysim", {"params": {}})
    api.invoke("dse_synth", {"params": [{}]})
    assert api.results()["candidates"][0]["feasible"] is None


POINT = {"ntap": 16, "samp_w": 16, "unroll_lane": True}


def test_restart_normalization_and_concurrent_cache(tmp_path):
    api = service(tmp_path)
    first = api.invoke("dse_synth", {"params": [POINT]})["rows"][0]
    assert first["status"] == "ok" and not first["cached"]
    with ThreadPoolExecutor(max_workers=4) as pool:
        rows = list(pool.map(lambda _: service(tmp_path).invoke(
            "dse_synth", {"params": [{**POINT, "samp_i": 2, "mem_dwidth": 32}]}), range(4)))
    assert all(row["rows"][0]["cached"] for row in rows)
    assert {row["rows"][0]["evaluation_id"] for row in rows} == {first["evaluation_id"]}
    assert service(tmp_path).context()["budget"]["spent"]["synth"] == 1


def test_batch_accounts_every_point_and_preserves_failures(tmp_path):
    api = service(tmp_path, {"budget": {"synth": 1}})
    points = [POINT, {**POINT, "ntap": 32}, {**POINT, "samp_w": 7}]
    batch = api.invoke("dse_synth", {"params": points})
    assert batch["requested_count"] == batch["returned_count"] == 3
    assert [r["status"] for r in batch["rows"]] == ["ok", "budget_exhausted", "invalid"]
    assert api.context()["budget"]["spent"]["synth"] == 1
    assert api.results()["total"] == 3
    assert api.results(limit=1)["has_more"]


def test_predictions_cannot_establish_feasibility(tmp_path):
    api = service(tmp_path)
    api.invoke("dse_pysim", {"params": POINT})
    api.invoke("dse_predict_resource", {"params": POINT})
    assert api.results()["candidates"][0]["feasible"] is None
    api.invoke("dse_synth", {"params": [POINT]})
    row = api.results()["candidates"][0]
    assert row["feasible"] is True
    assert row["discrepancies"]["top_dsp"] == {"predicted": 30, "replayed": 32, "delta": 2}
    assert api.context()["best_feasible"]["candidate_id"] == row["candidate_id"]
    api.invoke("dse_rtlsim", {"params": [POINT]})
    assert api.results()["candidates"][0]["observations"]["rtlsim"]["status"] == "unavailable"


def test_scope_cannot_change_on_resume(tmp_path):
    service(tmp_path)
    with pytest.raises(ValueError, match="configuration"):
        service(tmp_path, {"evaluation": {"seed": 5}})


def test_invalid_arguments_are_rows_not_exceptions(tmp_path):
    api = service(tmp_path)
    assert api.invoke("dse_pysim", {"params": {**POINT, "samp_w": True}})["status"] == "invalid"
    assert api.invoke("dse_pysim", {"params": POINT, "command": "bad"})["status"] == "invalid"
    assert api.invoke("not_a_tool", {})["status"] == "invalid"
    assert api.invoke("dse_synth", {"params": []})["status"] == "invalid"
    assert api.results()["total"] == 4


def test_quality_configuration_affects_identity(tmp_path):
    a = service(tmp_path / "a").invoke("dse_pysim", {"params": POINT})
    b = service(tmp_path / "b", {"evaluation": {"seed": 2}}).invoke("dse_pysim", {"params": POINT})
    assert a["evaluation_id"] != b["evaluation_id"]
    assert a["candidate_id"] == b["candidate_id"]


def test_invalid_requests_cannot_poison_valid_cache(tmp_path):
    api = service(tmp_path)
    point = Candidate.model_validate(POINT).model_dump()
    assert api.invoke("dse_pysim", point)["status"] == "invalid"
    assert api.invoke("dse_pysim", {"params": point})["status"] == "ok"


def test_nonfinite_and_scalar_requests_are_durable_invalid_rows(tmp_path):
    api = service(tmp_path)
    for name, args in [(None, {}), (42, {}), ("dse_pysim", 5),
                       ("dse_pysim", {"params": {"ntap": float("nan")}}),
                       ("dse_synth", {"params": [{"samp_w": float("inf")}]})]:
        result = api.invoke(name, args)
        rows = result.get("rows", [result])
        assert all(row["status"] == "invalid" for row in rows)
        json.dumps(result, allow_nan=False)
    assert api.context()["budget"]["spent"]["pysim"] == 0


@pytest.mark.parametrize("field,value", [("ntap", 16.0), ("samp_w", 16.0), ("mem_dwidth", 32.0)])
def test_literal_integers_are_strict(tmp_path, field, value):
    api = service(tmp_path)
    assert api.invoke("dse_pysim", {"params": {**POINT, field: value}})["status"] == "invalid"


def test_uncached_contention_charges_once_and_cache_costs_zero(tmp_path):
    api = service(tmp_path, {"budget": {"pysim": 1}})
    with ThreadPoolExecutor(max_workers=4) as pool:
        rows = list(pool.map(lambda n: api.invoke("dse_pysim", {"params": {**POINT, "ntap": n}}),
                             [16, 16, 32, 32]))
    assert sum(row["cost"]["units"] for row in rows) == 1
    assert api.context()["budget"]["spent"]["pysim"] == 1
    assert api.results()["total"] == 2


def test_sparse_metrics_do_not_break_feasibility_or_json(tmp_path):
    class SparseBackend(Backend):
        def synth(self, params):
            return {"status": "ok", "evidence_kind": "hls_estimate_replay",
                    "metrics": {"top_dsp": None, "top_lut": 1200}}

    api = DseService(tmp_path, backend=SparseBackend(), evaluator=quality, evaluator_identity="test-quality-v1")
    api.invoke("dse_pysim", {"params": POINT})
    api.invoke("dse_synth", {"params": [POINT]})
    assert api.context()["best_feasible"] is None
    assert api.results()["candidates"][0]["feasible"] is None
    json.dumps(api.results(), allow_nan=False)


@pytest.mark.parametrize("metrics", [None, [], {"stopband_rej_db": float("nan")}])
def test_malformed_evaluator_payloads_become_cached_failures(tmp_path, metrics):
    api = DseService(tmp_path, backend=Backend(), evaluator=lambda p, e: metrics, evaluator_identity="test-malformed-v1")
    row = api.invoke("dse_pysim", {"params": POINT})
    assert row["status"] == "failed"
    assert api.invoke("dse_pysim", {"params": POINT})["cached"]
    assert api.context()["budget"]["spent"]["pysim"] == 1
    json.dumps(api.results(), allow_nan=False)


def test_context_budget_and_candidates_share_a_snapshot(tmp_path, monkeypatch):
    api = service(tmp_path)
    read_rows = api.store.rows

    def interleaved_read():
        rows = read_rows()
        api.invoke("dse_pysim", {"params": POINT})
        return rows

    monkeypatch.setattr(api.store, "rows", interleaved_read)
    context = api.context()
    assert context["candidate_count"] == context["budget"]["spent"]["pysim"] == 0


@pytest.mark.parametrize("estimate,replayed", [("unknown", 32), (-1e308, 1e308)])
def test_discrepancies_skip_unusable_values(tmp_path, estimate, replayed):
    class PredictionBackend(Backend):
        def synth(self, params):
            return {"status": "ok", "evidence_kind": "hls_estimate_replay", "metrics": {"top_dsp": replayed}}

        def predict_resource(self, params):
            return {"status": "ok", "evidence_kind": "prediction", "metrics": {"top_dsp": {"est": estimate}}}

    api = DseService(tmp_path, backend=PredictionBackend(), evaluator=quality, evaluator_identity="test-quality-v1")
    api.invoke("dse_synth", {"params": [POINT]})
    api.invoke("dse_predict_resource", {"params": POINT})
    assert api.results()["candidates"][0]["discrepancies"] == {}
    json.dumps(api.results(), allow_nan=False)


def test_interrupted_local_evaluation_rolls_back_budget(tmp_path):
    def interrupt(params, evaluation):
        raise KeyboardInterrupt

    api = DseService(tmp_path, backend=Backend(), evaluator=interrupt, evaluator_identity="test-quality-v1")
    with pytest.raises(KeyboardInterrupt):
        api.invoke("dse_pysim", {"params": POINT})
    resumed = service(tmp_path)
    assert resumed.results()["total"] == 0
    assert resumed.context()["budget"]["spent"]["pysim"] == 0
    assert resumed.invoke("dse_pysim", {"params": POINT})["status"] == "ok"
