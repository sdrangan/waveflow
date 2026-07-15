import json
from pathlib import Path

from examples.regmap.simp_fun import DEFAULT_VECTOR, Int32, run_functional_cases
from examples.regmap.simp_fun_build import build_simp_fun_dag
from examples.regmap.timing_diagram import write_timing_diagram
from waveflow.build.build import BuildConfig


def test_simp_fun_python_sim_matches_expected_outputs(tmp_path: Path) -> None:
    # Stage 2: the golden + py_timing now come from running the single-process ``SeqTB`` (the
    # same ``main()`` that lowers to the C++ testbench), not from the retired ``SimpFunHost``.
    results = build_simp_fun_dag().run(
        BuildConfig(root_dir=tmp_path, params={
            "x": DEFAULT_VECTOR["x"],
            "a": DEFAULT_VECTOR["a"],
            "b": DEFAULT_VECTOR["b"],
        }),
        through="py_sim",
    )

    assert results["build_inputs"].success
    assert results["py_sim"].success

    y = int(Int32().read_uint32_file(results["py_sim"].path("sim_dir") / "y.bin").val)
    py_timing = json.loads(results["py_sim"].path("py_timing").read_text(encoding="utf-8"))

    assert y == 11
    assert py_timing["source"] == "seq_tb"
    # The SeqTB times just the kernel transaction: ``run_once_sim`` drives ``on_start``, whose
    # 4-cycle compute-latency timeout is the whole bracketed interval (the timer wraps only the
    # invocation, not the host-side regmap set/poll overhead the old SimpFunHost path included).
    assert py_timing["transaction_cycles"] == 4


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
