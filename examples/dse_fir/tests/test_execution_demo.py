"""A real cross-design demonstration, not synthetic report fixtures."""
import json
import subprocess
import sys


def test_demo_produces_real_evidence_and_refuses_overwrite(tmp_path):
    output = tmp_path / "demo"
    command = [sys.executable, "-m", "examples.dse_fir.execution.demo", "--output", str(output)]
    result = subprocess.run(command, capture_output=True, text=True, check=False, timeout=90)
    assert result.returncode == 0, result.stderr + result.stdout
    receipt = json.loads(result.stdout)
    bundle = json.loads((output / "study.json").read_text())
    assert receipt["passed"] is True
    assert {e["design_id"] for e in bundle["experiments"]} == {"fir", "mem_copy"}
    for experiment in bundle["experiments"]:
        runs = experiment["runs"]
        simulations = [r for r in runs if r["operation"] == "pysim" and r["status"] == "ok"]
        assert len(simulations) == 3
        assert simulations[1]["request_id"] == simulations[2]["request_id"]
        assert simulations[1]["run_id"] != simulations[2]["run_id"]
        assert any(r["operation"] == "synth" and r["status"] == "unavailable" for r in runs)
        assert any(r["operation"] == "rtlsim" and r["status"] == "invalid" for r in runs)
        assert any(r["operation"] == "pysim" and r["status"] == "invalid" for r in runs)
        for run in runs:
            path = output / experiment["design_id"] / "runs" / run["run_id"] / "observation.json"
            assert json.loads(path.read_text()) == run
        assert all(r["metrics"] for r in simulations)
    page = (output / "review.html").read_text()
    assert 'id="study-data"' in page
    assert bundle["experiments"][0]["experiment_id"] in page
    assert bundle["experiments"][1]["experiment_id"] in page
    again = subprocess.run(command, capture_output=True, text=True, check=False, timeout=15)
    assert again.returncode != 0
    assert json.loads((output / "study.json").read_text()) == bundle
