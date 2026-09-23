"""Real committed-corpus checks; no HLS or RTL tools are run."""
import json

from examples.fir_block.fir_block_corpus import GRID


def test_replays_all_and_only_frozen_hls_grid():
    from examples.dse_fir.backends import ReplayBackend

    backend = ReplayBackend()
    assert backend.capabilities()["synth"]["points"] == len(GRID) == 24
    for (ntap, width, unroll), expected in GRID.items():
        params = {"ntap": ntap, "samp_w": width, "samp_i": 2,
                  "unroll_lane": unroll, "mem_dwidth": 32}
        result = backend.synth(params)
        assert result["status"] == "ok"
        assert result["evidence_kind"] == "hls_estimate_replay"
        assert result["metrics"] == {k: v for k, v in expected.items() if k.startswith("top_")}
        assert result["provenance"]["module_key"].startswith("fir_compute-")
        json.dumps(result, allow_nan=False)
    params = {"ntap": 16, "samp_w": 16, "samp_i": 3, "unroll_lane": False, "mem_dwidth": 32}
    assert backend.synth(params)["status"] == "out_of_corpus"
    params.update(samp_i=2, mem_dwidth=64)
    assert backend.synth(params)["status"] == "out_of_corpus"


def test_rtl_never_borrows_pysim_or_platform_bus_evidence():
    from examples.dse_fir.backends import ReplayBackend

    result = ReplayBackend().rtlsim({"ntap": 16, "samp_w": 16, "samp_i": 2,
                                    "unroll_lane": False, "mem_dwidth": 32})
    assert result["status"] == "unavailable"
    assert result["evidence_kind"] == "none"
    assert result["metrics"] == {}


def test_prediction_uses_real_composition_and_reports_actual_support():
    from examples.dse_fir.backends import ReplayBackend
    from examples.fir_block.fir_block import FirBlock
    from waveflow.build.elaborate import elaborate
    from waveflow.calib.resource_model import compose

    backend = ReplayBackend()
    p = {"ntap": 16, "samp_w": 16, "samp_i": 2, "unroll_lane": False, "mem_dwidth": 32}
    top = elaborate(FirBlock, p, name="fir_block")
    top.add_rm(backend.platform)
    expected = compose(top)
    result = backend.predict_resource(p)
    assert result["status"] == "ok"
    assert result["evidence_kind"] == "resource_model_prediction"
    for counter in ("dsp", "lut", "ff", "bram"):
        metric = result["metrics"]["top_" + counter]
        assert metric["est"] == expected.total[counter]
        assert metric["exact"] == (counter in {"dsp", "bram"})
        assert not metric["extrapolating"]
    lut = result["metrics"]["top_lut"]
    compute_conf = next(c for _, name, _, c in expected.per_module if name == "FirCompute")
    facts = compute_conf.facts["per_target"]["lut"]
    assert lut["n_support"] == facts["n_points"]
    assert lut["max_abs_residual"] == facts["max_abs_residual"]
    assert lut["interval"] is None  # residual extrema are NOT a predictive confidence interval
    assert lut["uncertainty"] == "unknown_predictive_interval"
    json.dumps(result, allow_nan=False)


def test_off_domain_or_missing_lookup_never_returns_a_trusted_zero():
    from examples.dse_fir.backends import ReplayBackend

    backend = ReplayBackend()
    p = {"ntap": 16, "samp_w": 16, "samp_i": 2, "unroll_lane": False, "mem_dwidth": 32}
    for change in ({"samp_i": 3}, {"mem_dwidth": 128}, {"samp_w": 10}):
        result = backend.predict_resource({**p, **change})
        assert result["status"] == "uncalibrated"
        assert result["metrics"] == {}
    result = backend.predict_resource({**p, "ntap": 256})
    assert result["status"] == "ok"
    assert result["metrics"]["top_lut"]["extrapolating"]
    assert not result["metrics"]["top_lut"]["exact"]
    assert result["metrics"]["top_dsp"]["extrapolating"]
    assert not result["metrics"]["top_dsp"]["exact"]


def test_capabilities_enumerate_real_replay_points_and_record_provenance():
    from examples.dse_fir.backends import ReplayBackend

    backend = ReplayBackend()
    points = backend.capabilities()["synth"]["supported_params"]
    assert {(p["ntap"], p["samp_w"], p["unroll_lane"]) for p in points} == set(GRID)
    result = backend.synth(points[0])
    path = "calibration/" + result["provenance"]["record_path"]
    assert result["provenance"]["record_sha256"] == backend.identity["source_hashes"][path]


def test_identity_tracks_content_not_mtime_and_refuses_midlife_change(tmp_path):
    import os
    import shutil

    import pytest

    from examples.dse_fir.backends import ReplayBackend
    from examples.fir_block.fir_block_corpus import COMMITTED_CALIB

    copied = tmp_path / COMMITTED_CALIB.name
    shutil.copytree(COMMITTED_CALIB, copied)
    a, b = ReplayBackend(copied), ReplayBackend(copied)
    assert a.identity == b.identity == ReplayBackend().identity
    record = next(copied.glob("modules/fir_compute-*/resource/records.jsonl"))
    stat = record.stat()
    record.write_text(record.read_text() + "\n")
    os.utime(record, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert ReplayBackend(copied).identity != a.identity
    with pytest.raises(RuntimeError, match="content changed"):
        a.synth({"ntap": 16, "samp_w": 16, "samp_i": 2, "unroll_lane": False, "mem_dwidth": 32})


def test_wrong_exact_module_identity_cannot_replay_grid_values(tmp_path):
    import shutil

    from examples.dse_fir.backends import ReplayBackend
    from examples.fir_block.fir_block_corpus import COMMITTED_CALIB

    copied = tmp_path / COMMITTED_CALIB.name
    shutil.copytree(COMMITTED_CALIB, copied)
    manifest = copied / "modules/fir_compute-0295b0c7/module.json"
    data = json.loads(manifest.read_text())
    data["params"]["samp_i"] = 3
    manifest.write_text(json.dumps(data))
    result = ReplayBackend(copied).synth({"ntap": 8, "samp_w": 16, "samp_i": 2,
                                        "unroll_lane": False, "mem_dwidth": 32})
    assert result["status"] == "unavailable"
    assert result["metrics"] == {}


def test_missing_platform_is_not_created(tmp_path):
    import pytest

    from examples.dse_fir.backends import ReplayBackend

    missing = tmp_path / "no-library"
    with pytest.raises(FileNotFoundError):
        ReplayBackend(missing)
    assert not missing.exists()


def test_missing_child_record_is_uncalibrated_not_zero(tmp_path):
    import shutil

    from examples.dse_fir.backends import ReplayBackend
    from examples.fir_block.fir_block_corpus import COMMITTED_CALIB

    copied = tmp_path / COMMITTED_CALIB.name
    shutil.copytree(COMMITTED_CALIB, copied)
    (copied / "modules/mem_w_stream-51d74266/resource/records.jsonl").unlink()
    result = ReplayBackend(copied).predict_resource({"ntap": 16, "samp_w": 16, "samp_i": 2,
                                                    "unroll_lane": False, "mem_dwidth": 32})
    assert result["status"] == "uncalibrated"
    assert result["metrics"] == {}
