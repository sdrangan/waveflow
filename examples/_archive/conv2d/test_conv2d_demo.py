import json
import re
import shutil
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pytest

# conv2d is archived (examples/_archive/) pending a systolic-array rewrite as a proper
# HwComponent; it uses the retired serialization API + pre-component HLS style and is not
# in CI. This whole module is skipped, and pytest is configured (norecursedirs +
# --ignore-glob in pyproject.toml) not to collect anything under examples/_archive.
pytestmark = pytest.mark.skip(reason="conv2d archived pending systolic-array rework")

from examples._archive.conv2d.conv2d_demo import (  # noqa: E402
    Conv2DError,
    Conv2DSimResult,
    Conv2DTest,
    PixelField,
)
from waveflow.hw.arrayutils import write_uint32_file  # noqa: E402
from waveflow.toolchain import toolchain  # noqa: E402


CONV2D_EXAMPLE_DIR = Path(__file__).resolve().parent


def _copy_conv2d_vitis_resources(dst_dir: Path) -> None:
    for name in ("conv2d.cpp", "conv2d.hpp", "conv2d_tb.cpp", "run.tcl"):
        shutil.copy(CONV2D_EXAMPLE_DIR / name, dst_dir / name)


def test_conv2d_header_limits_match_python_harness_limits() -> None:
    header_text = (CONV2D_EXAMPLE_DIR / "conv2d.hpp").read_text(encoding="utf-8")
    max_nrow_match = re.search(r"static const int max_nrow = (\d+);", header_text)
    max_ncol_match = re.search(r"static const int max_ncol = (\d+);", header_text)

    assert max_nrow_match is not None
    assert max_ncol_match is not None
    assert int(max_nrow_match.group(1)) == 512
    assert int(max_ncol_match.group(1)) == 512


@pytest.mark.parametrize("mem_dwidth", [32, 64, 128])
def test_conv2d_test_simulate_matches_expected_output(mem_dwidth: int) -> None:
    conv2d_test = Conv2DTest(seed=11, nrows=7, ncols=9, kernel_size=3, mem_dwidth=mem_dwidth)

    result = conv2d_test.simulate()

    assert conv2d_test.mem is not None
    assert conv2d_test.conv2d_accel is not None
    assert conv2d_test.cmd is not None
    assert conv2d_test.resp is not None
    assert conv2d_test.im_out is not None
    assert conv2d_test.im_out_expected is not None

    assert conv2d_test.mem.word_size == mem_dwidth
    assert result.cmd is conv2d_test.cmd
    assert result.resp is conv2d_test.resp
    assert result.im_out is conv2d_test.im_out
    assert result.im_out_expected is conv2d_test.im_out_expected
    assert result.passed is True

    assert conv2d_test.resp.error_code is Conv2DError.NO_ERROR
    assert conv2d_test.im_out.dtype == np.uint8
    assert conv2d_test.im_out_expected.dtype == np.uint8
    assert conv2d_test.im_out.shape == (7, 9)
    assert np.array_equal(conv2d_test.im_out, conv2d_test.im_out_expected)


def test_conv2d_test_gen_test_data_initializes_state_before_simulate() -> None:
    conv2d_test = Conv2DTest(seed=5, nrows=6, ncols=8, kernel_size=4, mem_dwidth=64)

    conv2d_test.gen_test_data()

    assert conv2d_test.cmd is None
    assert conv2d_test.im_in is not None
    assert conv2d_test.kernel is not None
    assert conv2d_test.im_in.shape == (6, 8)
    assert conv2d_test.im_in.dtype == np.uint8
    assert conv2d_test.kernel.shape == (4, 4)
    assert np.issubdtype(conv2d_test.kernel.dtype, np.signedinteger)

    result = conv2d_test.simulate()

    assert conv2d_test.cmd is not None
    assert result.passed is True


def test_conv2d_test_write_input_files_and_read_vitis_outputs(tmp_path: Path) -> None:
    conv2d_test = Conv2DTest(seed=13, nrows=5, ncols=6, kernel_size=3, example_dir=tmp_path)
    expected_result = conv2d_test.simulate()

    data_dir = conv2d_test.write_input_files(tmp_path / "data")

    params = json.loads((data_dir / "params.json").read_text(encoding="utf-8"))
    assert params == {"nrows": 5, "ncols": 6, "kernel_size": 3}
    assert (data_dir / "im_in_array.bin").exists()
    assert (data_dir / "kernel_array.bin").exists()

    assert conv2d_test.resp is not None
    conv2d_test.resp.write_uint32_file(data_dir / "resp_data.bin")
    write_uint32_file(
        expected_result.im_out,
        elem_type=PixelField,
        file_path=data_dir / "im_out_array.bin",
    )

    vitis_result = conv2d_test.read_vitis_outputs(data_dir)

    assert vitis_result.cmd is conv2d_test.cmd
    assert vitis_result.resp.error_code is Conv2DError.NO_ERROR
    assert vitis_result.im_out.dtype == np.uint8
    assert vitis_result.passed is True


def test_conv2d_test_vitis_stage_range_passes_tcl_args(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _copy_conv2d_vitis_resources(tmp_path)
    conv2d_test = Conv2DTest(seed=11, nrows=7, ncols=9, kernel_size=3, example_dir=tmp_path)
    expected_result = conv2d_test.simulate()

    captured: dict[str, object] = {}

    def fake_gen_vitis_code() -> list[Path]:
        return []

    def fake_write_input_files(data_dir: Path | None = None) -> Path:
        target_dir = tmp_path / "data"
        target_dir.mkdir(parents=True, exist_ok=True)
        return target_dir

    def fake_run_vitis_hls(
        run_tcl: Path,
        work_dir: Path,
        capture_output: bool,
        env: dict[str, str] | None = None,
    ):
        captured["run_tcl"] = run_tcl
        captured["work_dir"] = work_dir
        captured["capture_output"] = capture_output
        captured["env"] = env
        return SimpleNamespace(stdout="conv2d csynth ok", stderr="")

    def fake_read_vitis_outputs(data_dir: Path, validate: bool = True):
        captured["data_dir"] = data_dir
        captured["validate"] = validate
        return expected_result

    monkeypatch.setattr(conv2d_test, "gen_vitis_code", fake_gen_vitis_code)
    monkeypatch.setattr(conv2d_test, "write_input_files", fake_write_input_files)
    monkeypatch.setattr(conv2d_test, "read_vitis_outputs", fake_read_vitis_outputs)
    monkeypatch.setattr(toolchain, "run_vitis_hls", fake_run_vitis_hls)

    result = conv2d_test.test_vitis(start_at="csim", through="csynth")

    assert result is expected_result
    assert captured["run_tcl"] == tmp_path / "run.tcl"
    assert captured["work_dir"] == tmp_path
    assert captured["capture_output"] is True
    assert captured["data_dir"] == tmp_path / "data"
    assert captured["env"] == {
        "WAVEFLOW_CONV2D_START_AT": "csim",
        "WAVEFLOW_CONV2D_THROUGH": "csynth",
        "WAVEFLOW_CONV2D_TRACE_LEVEL": "none",
    }
    assert captured["validate"] is True


def test_conv2d_test_vitis_csim_clears_project_and_logs_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _copy_conv2d_vitis_resources(tmp_path)
    conv2d_test = Conv2DTest(seed=11, nrows=7, ncols=9, kernel_size=3, example_dir=tmp_path)
    expected_result = conv2d_test.simulate()

    project_dir = tmp_path / "waveflow_conv2d_proj"
    logs_dir = tmp_path / "logs"
    include_dir = tmp_path / "include"
    project_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)
    include_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "stale.txt").write_text("project", encoding="utf-8")
    (logs_dir / "stale.txt").write_text("logs", encoding="utf-8")
    (include_dir / "stale.txt").write_text("include", encoding="utf-8")

    captured: dict[str, object] = {}

    def fake_gen_vitis_code() -> list[Path]:
        captured["project_exists"] = project_dir.exists()
        captured["logs_exists"] = logs_dir.exists()
        captured["include_exists"] = include_dir.exists()
        captured["include_stale_exists"] = (include_dir / "stale.txt").exists()
        return []

    def fake_write_input_files(data_dir: Path | None = None) -> Path:
        target_dir = tmp_path / "data"
        target_dir.mkdir(parents=True, exist_ok=True)
        return target_dir

    def fake_run_vitis_hls(run_tcl: Path, work_dir: Path, capture_output: bool, env: dict[str, str] | None = None):
        return SimpleNamespace(stdout="conv2d csim ok", stderr="")

    def fake_read_vitis_outputs(data_dir: Path, validate: bool = True):
        return expected_result

    monkeypatch.setattr(conv2d_test, "gen_vitis_code", fake_gen_vitis_code)
    monkeypatch.setattr(conv2d_test, "write_input_files", fake_write_input_files)
    monkeypatch.setattr(conv2d_test, "read_vitis_outputs", fake_read_vitis_outputs)
    monkeypatch.setattr(toolchain, "run_vitis_hls", fake_run_vitis_hls)

    result = conv2d_test.test_vitis(start_at="csim", through="csim")

    assert result is expected_result
    assert captured == {
        "project_exists": False,
        "logs_exists": False,
        "include_exists": True,
        "include_stale_exists": True,
    }


def test_conv2d_test_vitis_csim_reset_includes_clears_include_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _copy_conv2d_vitis_resources(tmp_path)
    conv2d_test = Conv2DTest(seed=11, nrows=7, ncols=9, kernel_size=3, example_dir=tmp_path)
    expected_result = conv2d_test.simulate()

    project_dir = tmp_path / "waveflow_conv2d_proj"
    logs_dir = tmp_path / "logs"
    include_dir = tmp_path / "include"
    project_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)
    include_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "stale.txt").write_text("project", encoding="utf-8")
    (logs_dir / "stale.txt").write_text("logs", encoding="utf-8")
    (include_dir / "stale.txt").write_text("include", encoding="utf-8")

    captured: dict[str, object] = {}

    def fake_gen_vitis_code() -> list[Path]:
        captured["project_exists"] = project_dir.exists()
        captured["logs_exists"] = logs_dir.exists()
        captured["include_exists"] = include_dir.exists()
        captured["include_stale_exists"] = (include_dir / "stale.txt").exists()
        return []

    def fake_write_input_files(data_dir: Path | None = None) -> Path:
        target_dir = tmp_path / "data"
        target_dir.mkdir(parents=True, exist_ok=True)
        return target_dir

    def fake_run_vitis_hls(run_tcl: Path, work_dir: Path, capture_output: bool, env: dict[str, str] | None = None):
        return SimpleNamespace(stdout="conv2d csim ok", stderr="")

    def fake_read_vitis_outputs(data_dir: Path, validate: bool = True):
        return expected_result

    monkeypatch.setattr(conv2d_test, "gen_vitis_code", fake_gen_vitis_code)
    monkeypatch.setattr(conv2d_test, "write_input_files", fake_write_input_files)
    monkeypatch.setattr(conv2d_test, "read_vitis_outputs", fake_read_vitis_outputs)
    monkeypatch.setattr(toolchain, "run_vitis_hls", fake_run_vitis_hls)

    result = conv2d_test.test_vitis(start_at="csim", through="csim", reset_includes=True)

    assert result is expected_result
    assert captured == {
        "project_exists": False,
        "logs_exists": False,
        "include_exists": False,
        "include_stale_exists": False,
    }


def test_conv2d_test_vitis_rejects_invalid_stage_range() -> None:
    conv2d_test = Conv2DTest()

    with pytest.raises(ValueError, match="must not come after"):
        conv2d_test.test_vitis(start_at="cosim", through="csynth")


def test_conv2d_test_generate_vcd_clamps_vitis_through_to_cosim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _copy_conv2d_vitis_resources(tmp_path)
    conv2d_test = Conv2DTest(seed=11, nrows=7, ncols=9, kernel_size=3, example_dir=tmp_path)
    expected_result = conv2d_test.simulate()

    captured: dict[str, object] = {}

    def fake_gen_vitis_code() -> list[Path]:
        return []

    def fake_write_input_files(data_dir: Path | None = None) -> Path:
        target_dir = tmp_path / "data"
        target_dir.mkdir(parents=True, exist_ok=True)
        return target_dir

    def fake_run_vitis_hls(
        run_tcl: Path,
        work_dir: Path,
        capture_output: bool,
        env: dict[str, str] | None = None,
    ):
        captured["env"] = env
        return SimpleNamespace(stdout="conv2d cosim ok", stderr="")

    def fake_read_vitis_outputs(data_dir: Path, validate: bool = True):
        return expected_result

    def fake_generate_vcd(output_vcd: str = "dump.vcd", soln: str | None = "solution1", trace_level: str = "*") -> Path:
        captured["generate_vcd"] = {
            "output_vcd": output_vcd,
            "soln": soln,
            "trace_level": trace_level,
        }
        return tmp_path / "vcd" / output_vcd

    monkeypatch.setattr(conv2d_test, "gen_vitis_code", fake_gen_vitis_code)
    monkeypatch.setattr(conv2d_test, "write_input_files", fake_write_input_files)
    monkeypatch.setattr(conv2d_test, "read_vitis_outputs", fake_read_vitis_outputs)
    monkeypatch.setattr(conv2d_test, "generate_vcd", fake_generate_vcd)
    monkeypatch.setattr(toolchain, "run_vitis_hls", fake_run_vitis_hls)

    result = conv2d_test.test_vitis(start_at="csim", through="generate_vcd", trace_level="port")

    assert result is expected_result
    assert captured["env"] == {
        "WAVEFLOW_CONV2D_START_AT": "csim",
        "WAVEFLOW_CONV2D_THROUGH": "cosim",
        "WAVEFLOW_CONV2D_TRACE_LEVEL": "port",
    }
    assert captured["generate_vcd"] == {
        "output_vcd": "dump.vcd",
        "soln": "solution1",
        "trace_level": "port",
    }


def test_conv2d_test_generate_vcd_stage_skips_vitis(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _copy_conv2d_vitis_resources(tmp_path)
    conv2d_test = Conv2DTest(seed=11, nrows=7, ncols=9, kernel_size=3, example_dir=tmp_path)
    project_dir = tmp_path / "waveflow_conv2d_proj" / "solution1"
    project_dir.mkdir(parents=True, exist_ok=True)

    captured: dict[str, object] = {}

    def fake_generate_vcd(output_vcd: str = "dump.vcd", soln: str | None = "solution1", trace_level: str = "*") -> Path:
        captured["output_vcd"] = output_vcd
        captured["soln"] = soln
        captured["trace_level"] = trace_level
        return tmp_path / "vcd" / output_vcd

    def fail_run_vitis_hls(*args, **kwargs):
        raise AssertionError("Vitis should not be invoked for the generate_vcd-only stage.")

    monkeypatch.setattr(conv2d_test, "generate_vcd", fake_generate_vcd)
    monkeypatch.setattr(toolchain, "run_vitis_hls", fail_run_vitis_hls)

    result = conv2d_test.test_vitis(start_at="generate_vcd", through="generate_vcd")

    assert result is None
    assert captured == {
        "output_vcd": "dump.vcd",
        "soln": "solution1",
        "trace_level": "*",
    }


def test_conv2d_test_generate_vcd_stage_uses_df_dump_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _copy_conv2d_vitis_resources(tmp_path)
    conv2d_test = Conv2DTest(
        seed=11,
        nrows=7,
        ncols=9,
        kernel_size=3,
        example_dir=tmp_path,
        use_df=True,
    )
    project_dir = tmp_path / "waveflow_conv2d_df_proj" / "solution1"
    project_dir.mkdir(parents=True, exist_ok=True)

    captured: dict[str, object] = {}

    def fake_generate_vcd(output_vcd: str | None = None, soln: str | None = "solution1", trace_level: str = "*") -> Path:
        captured["output_vcd"] = output_vcd
        captured["soln"] = soln
        captured["trace_level"] = trace_level
        return tmp_path / "vcd" / (output_vcd or "")

    def fail_run_vitis_hls(*args, **kwargs):
        raise AssertionError("Vitis should not be invoked for the generate_vcd-only stage.")

    monkeypatch.setattr(conv2d_test, "generate_vcd", fake_generate_vcd)
    monkeypatch.setattr(toolchain, "run_vitis_hls", fail_run_vitis_hls)

    result = conv2d_test.test_vitis(start_at="generate_vcd", through="generate_vcd")

    assert result is None
    assert captured == {
        "output_vcd": None,
        "soln": "solution1",
        "trace_level": "*",
    }


def test_conv2d_generate_vcd_defaults_to_df_dump_name(monkeypatch: pytest.MonkeyPatch) -> None:
    conv2d_test = Conv2DTest(seed=11, nrows=7, ncols=9, kernel_size=3, use_df=True)
    captured: dict[str, object] = {}

    def fake_run_xsim_vcd(*, top, comp, out, soln, trace_level, workdir):
        captured["top"] = top
        captured["comp"] = comp
        captured["out"] = out
        captured["soln"] = soln
        captured["trace_level"] = trace_level
        captured["workdir"] = workdir
        return Path(workdir) / "vcd" / out

    monkeypatch.setattr("waveflow.scripts.xsim_vcd.run_xsim_vcd", fake_run_xsim_vcd)

    vcd_path = conv2d_test.generate_vcd()

    assert captured["top"] == "conv2d_df"
    assert captured["comp"] == "waveflow_conv2d_df_proj"
    assert captured["out"] == "dump_df.vcd"
    assert captured["soln"] == "solution1"
    assert captured["trace_level"] == "*"
    assert captured["workdir"] == conv2d_test.example_dir
    assert vcd_path == conv2d_test.example_dir / "vcd" / "dump_df.vcd"


def test_conv2d_test_vitis_allows_csim_mismatches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _copy_conv2d_vitis_resources(tmp_path)
    conv2d_test = Conv2DTest(seed=11, nrows=7, ncols=9, kernel_size=3, example_dir=tmp_path)
    expected_result = conv2d_test.simulate()

    captured: dict[str, object] = {}

    def fake_write_input_files(data_dir: Path | None = None) -> Path:
        target_dir = tmp_path / "data"
        target_dir.mkdir(parents=True, exist_ok=True)
        return target_dir

    def fake_run_vitis_hls(run_tcl: Path, work_dir: Path, capture_output: bool, env: dict[str, str] | None = None):
        captured["env"] = env
        return SimpleNamespace(stdout="conv2d csim ok", stderr="")

    def fake_read_vitis_outputs(data_dir: Path, validate: bool = True):
        captured["validate"] = validate
        return expected_result

    monkeypatch.setattr(conv2d_test, "write_input_files", fake_write_input_files)
    monkeypatch.setattr(conv2d_test, "read_vitis_outputs", fake_read_vitis_outputs)
    monkeypatch.setattr(toolchain, "run_vitis_hls", fake_run_vitis_hls)

    result = conv2d_test.test_vitis(through="csim", allow_csim_errors=True)

    assert result is expected_result
    assert captured["validate"] is False
    assert captured["env"] == {
        "WAVEFLOW_CONV2D_START_AT": "csim",
        "WAVEFLOW_CONV2D_THROUGH": "csim",
        "WAVEFLOW_CONV2D_TRACE_LEVEL": "none",
    }


def test_conv2d_test_write_csynth_reports(tmp_path: Path) -> None:
    project_report_dir = tmp_path / "waveflow_conv2d_proj" / "solution1" / "syn" / "report"
    project_report_dir.mkdir(parents=True, exist_ok=True)
    (project_report_dir / "csynth.xml").write_text("<Report></Report>", encoding="utf-8")

    conv2d_test = Conv2DTest(example_dir=tmp_path)
    data_dir = tmp_path / "data"

    class FakeParser:
        def __init__(self, sol_path: str):
            assert sol_path == str(tmp_path / "waveflow_conv2d_proj" / "solution1")
            self.loop_df = None
            self.res_df = None

        def get_loop_pipeline_info(self):
            import pandas as pd
            self.loop_df = pd.DataFrame.from_records([
                {"PipelineII": 2, "PipelineDepth": 7},
            ], index=["conv2d_Pipeline_convolve_row:convolve_row"])

        def get_resources(self):
            import pandas as pd
            self.res_df = pd.DataFrame.from_records([
                {"DSP": 8, "LUT": 123},
            ], index=["Total"])

    with patch("examples._archive.conv2d.conv2d_demo.CsynthParser", FakeParser):
        outputs = conv2d_test.write_csynth_reports(data_dir)

    assert outputs is not None
    assert outputs["loop_csv"].exists()
    assert outputs["res_csv"].exists()
    assert outputs["loop_json"].exists()
    json_data = json.loads(outputs["loop_json"].read_text(encoding="utf-8"))
    assert json_data[0]["PipelineII"] == 2