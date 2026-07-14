import json
from pathlib import Path

from examples.regmap.simp_fun import DEFAULT_VECTOR, Int32, run_functional_cases
from examples.regmap.simp_fun_build import build_simp_fun_dag
from examples.regmap.timing_diagram import write_timing_diagram
from waveflow.build.build import BuildConfig


def test_simp_fun_python_sim_matches_expected_outputs(tmp_path: Path) -> None:
    results = build_simp_fun_dag().run(
        BuildConfig(root_dir=tmp_path, params={
            "x": DEFAULT_VECTOR["x"],
            "a": DEFAULT_VECTOR["a"],
            "b": DEFAULT_VECTOR["b"],
            "latency_cycles": 4,
        }),
        through="extract_py_timing",
    )

    assert results["build_inputs"].success
    assert results["py_sim"].success
    assert results["extract_py_timing"].success

    y = int(Int32().read_uint32_file(results["py_sim"].path("sim_dir") / "y.bin").val)
    summary = json.loads(results["py_sim"].path("sim_summary").read_text(encoding="utf-8"))
    py_timing = json.loads(results["extract_py_timing"].path("py_timing").read_text(encoding="utf-8"))

    assert y == 11
    assert summary["passed"] is True
    assert summary["ap_done"] == 1
    # Latency now lives in the kernel (on_start yields a 4-cycle timeout) and the host
    # genuinely polls ap_done instead of pre-waiting the exact kernel latency. The kernel
    # finishes at cycle 4; the host's first poll reads ap_done=0 and catches it on the
    # second poll (poll_interval=4 cycles), so the honest transaction latency is 6 cycles
    # (was 5, an artifact of the old pre-poll wait + single read).
    assert py_timing["transaction_cycles"] == 6


def test_simp_fun_codegen_emits_kernel_tb_and_impl(tmp_path: Path) -> None:
    dag = build_simp_fun_dag()
    kernel_results = dag.run(BuildConfig(root_dir=tmp_path), through="gen_kernel")
    tb_results = dag.run(BuildConfig(root_dir=tmp_path), through="gen_tb")

    assert kernel_results["gen_kernel"].success, kernel_results["gen_kernel"].message
    assert tb_results["gen_tb"].success, tb_results["gen_tb"].message
    assert (tmp_path / "gen" / "simp_fun.hpp").exists()
    assert (tmp_path / "gen" / "simp_fun.cpp").exists()
    assert (tmp_path / "gen" / "simp_fun_tb.cpp").exists()
    assert (tmp_path / "simp_fun_compute_impl.cpp").exists()


def test_write_timing_diagram_creates_svg_and_json(tmp_path: Path) -> None:
    py_path = tmp_path / "py_timing.json"
    cosim_path = tmp_path / "cosim_timing.json"
    verdict_path = tmp_path / "timing_verdict.json"
    py_path.write_text(json.dumps({"transaction_cycles": 4}), encoding="utf-8")
    cosim_path.write_text(json.dumps({"transaction_cycles": 5}), encoding="utf-8")
    verdict_path.write_text(json.dumps({
        "pass": True,
        "delta": 1,
        "tolerance": 4,
    }), encoding="utf-8")

    svg_path = tmp_path / "timing_diagram.svg"
    json_path = tmp_path / "timing_diagram.json"
    metadata = write_timing_diagram(py_path, cosim_path, verdict_path, svg_path, json_path)

    assert svg_path.exists()
    assert svg_path.stat().st_size > 0
    assert json_path.exists()
    assert metadata["events"][1]["cycle"] == 4
    assert metadata["events"][3]["cycle"] == 5


def test_run_functional_cases_covers_relu_and_signed_inputs() -> None:
    results = run_functional_cases()

    assert len(results) >= 3
    assert results[0]["y"] == 0
    assert results[1]["y"] == 11
    assert results[2]["y"] == 17
