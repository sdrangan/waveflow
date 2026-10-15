"""Real adapter boundary tests: workload separation and CPU evidence, not RTL."""
import json

import pytest


def test_fir_two_candidates_execute_quality_not_rtl(tmp_path):
    from examples.dse_fir.execution.designs import get_design

    design = get_design("fir", {"nsamp": 1024, "nfft": 2048})
    results = [design.pysim({"ntap": taps}, tmp_path / str(taps)) for taps in (8, 32)]
    for result in results:
        assert result["status"] == "ok"
        assert result["metrics"]["passband_sndr_db"]["unit"] == "dB"
        assert any(c["name"] == "rtl_equivalence" and c["status"] == "not_checked"
                   for c in result["checks"])
        json.dumps(result, allow_nan=False)
    assert results[0]["metrics"]["stopband_rej_db"]["value"] != results[1]["metrics"]["stopband_rej_db"]["value"]
    assert (tmp_path / "8" / "fir_evaluation.json").is_file()
    assert (tmp_path / "32" / "fir_evaluation.json").is_file()


def test_mem_copy_width_changes_real_work_in_words_not_bytes(tmp_path):
    from examples.dse_fir.execution.designs import get_design

    design = get_design("mem_copy", {"bytes_per_job": 512, "num_cmds": 4})
    runs = [design.pysim({"mem_dwidth": width}, tmp_path / str(width)) for width in (32, 64)]
    for width, run in zip((32, 64), runs):
        assert run["status"] == "ok"
        assert run["metrics"]["copied_bytes"]["value"] == 2048
        assert run["metrics"]["done_tokens"]["value"] == 4
        raw = json.loads((tmp_path / str(width) / "results/pysim.json").read_text())
        assert raw["n_words"] * (width // 8) == 512
        assert any(c["name"] == "destination_matches_source" and c["status"] == "passed"
                   for c in run["checks"])
        json.dumps(run, allow_nan=False)
    assert runs[0]["metrics"]["end_cycles"]["value"] != runs[1]["metrics"]["end_cycles"]["value"]


def test_mem_copy_corruption_is_failed_not_success(tmp_path, monkeypatch):
    from examples.dse_fir.execution.designs import get_design
    from examples.mem_copy.mem_copy_sim import MemCopySim

    original = MemCopySim.write_scenario

    def corrupt_expected(self, root):
        original(self, root)
        self.expected[0][0] ^= 1

    monkeypatch.setattr(MemCopySim, "write_scenario", corrupt_expected)
    run = get_design("mem_copy").pysim({}, tmp_path)
    assert run["status"] == "failed"
    assert any(c["status"] == "failed" for c in run["checks"])
    assert not run["metrics"]


@pytest.mark.parametrize("name,where,bad", [
    ("fir", "candidate", {"ntap": 8.0}),
    ("fir", "candidate", {"samp_i": True}),
    ("fir", "candidate", {"unroll_lane": 1}),
    ("fir", "candidate", {"seed": 1}),
    ("fir", "conditions", {"ntap": 8}),
    ("fir", "conditions", {"input_peak": True}),
    ("fir", "conditions", {"nsamp": True}),
    ("fir", "conditions", {"f_stop": float("nan")}),
    ("mem_copy", "candidate", {"mem_dwidth": 64.0}),
    ("mem_copy", "candidate", {"mem_dwidth": True}),
    ("mem_copy", "candidate", {"mem_dwidth": 128}),
    ("mem_copy", "candidate", {"bytes_per_job": 512}),
    ("mem_copy", "conditions", {"mem_dwidth": 32}),
    ("mem_copy", "conditions", {"bytes_per_job": 9}),
    ("mem_copy", "conditions", {"num_cmds": 100000}),
])
def test_host_and_candidate_domains_reject_malformed_values(name, where, bad):
    from examples.dse_fir.execution.designs import get_design

    with pytest.raises(ValueError):
        if where == "conditions":
            get_design(name, bad)
        else:
            get_design(name).validate(bad)


@pytest.mark.parametrize("name", ["fir", "mem_copy"])
def test_descriptors_are_stable_defensive_and_hardware_unavailable(name):
    from examples.dse_fir.execution.designs import get_design

    supplied = {"seed": 7} if name == "fir" else {"bytes_per_job": 512}
    design = get_design(name, supplied)
    original = design.describe()
    identity = design.identity
    supplied.clear()
    design.conditions.clear()
    design.identity.clear()
    description = design.describe()
    description["conditions"].clear()
    assert design.describe() == original
    assert design.identity == identity == get_design(name, original["conditions"]).identity
    assert identity["sources"] and identity["dependencies"]
    assert all(len(digest) == 64 for digest in identity["sources"].values())
    assert design.validate({}) == {k: v["default"] for k, v in original["parameter_schema"]["properties"].items()}
    json.dumps(original, allow_nan=False)
    for operation in ("synth", "rtlsim"):
        assert not original["capabilities"][operation]["available"]
        assert "not yet integrated" in original["capabilities"][operation]["reason"].lower()
