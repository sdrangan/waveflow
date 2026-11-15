from __future__ import annotations

import pytest

from waveflow.board import Board, ClockRequest, ClockRequestError
from waveflow.build.build import BuildConfig
from waveflow.build.composite_gen import render_tcl, tcl_target
from waveflow.toolchain import find_vivado_path, find_vitis_path, run_vitis_hls, tool_version
from waveflow.utils.csynthparse import synth_target


pytestmark = pytest.mark.vivado


@pytest.fixture
def board():
    if find_vivado_path() is None:
        pytest.skip("Vivado not available; no clock validation performed")
    return Board.rfsoc4x2()


def test_live_ps_roundtrip(board, tmp_path):
    first = board.resolve_clock(ClockRequest("ps_pl0", 100e6, 100e6), work_dir=tmp_path)
    second = board.resolve_clock(ClockRequest("ps_pl0", first.actual_hz, 100e6), work_dir=tmp_path)
    assert first.actual_hz == 99_999_985
    assert first.error_ppm == pytest.approx(0.15)
    assert second.actual_hz == first.actual_hz
    assert second.error_ppm == 0
    assert first.generator_parameters == second.generator_parameters
    assert first.log_path.is_file() and second.manifest_path.is_file()
    assert tcl_target(BuildConfig(board=board, resolved_clock=first)) == (board.part, 10)


def test_live_mmcm(board, tmp_path):
    clock = board.resolve_clock(ClockRequest("pl_ref_mmcm", 245.76e6, 250e6), work_dir=tmp_path)
    assert clock.actual_hz == 245.76e6
    assert clock.generator_parameters["multiplier"] == 9.75
    assert clock.generator_parameters["input_divider"] == 1
    assert clock.generator_parameters["output_divider"] == 4.875
    assert tcl_target(BuildConfig(board=board, resolved_clock=clock)) == (board.part, 4)


def test_live_rejection_keeps_diagnostics(board, tmp_path):
    with pytest.raises(ClockRequestError) as error:
        board.resolve_clock(ClockRequest("ps_pl0", 123.456e6, 125e6), work_dir=tmp_path)
    clock = error.value.resolution
    assert clock.actual_hz == 115_384_598
    assert not clock.accepted
    assert clock.manifest_path.is_file()
    with pytest.raises(ClockRequestError):
        BuildConfig(board=board, resolved_clock=clock)



@pytest.mark.vitis
def test_live_resolved_clock_reaches_hls_report(board, tmp_path):
    exe = find_vitis_path()
    if exe is None:
        pytest.skip("Vitis HLS not available; no synthesis-target validation performed")
    assert tool_version(exe, "--version") == board.vivado_version
    clock = board.resolve_clock(ClockRequest("ps_pl0", 100e6, 100e6), work_dir=tmp_path)
    cfg = BuildConfig(root_dir=tmp_path, board=board, resolved_clock=clock)
    top = "resolved_clock_probe"
    (tmp_path / "gen").mkdir()
    (tmp_path / "include").mkdir()
    (tmp_path / "gen" / f"{top}.cpp").write_text(
        f"unsigned {top}(unsigned x) {{ return x + 1; }}\n", encoding="utf-8",
    )
    part, period = tcl_target(cfg)
    script = tmp_path / "run.tcl"
    script.write_text(render_tcl(top, part=part, period_ns=period), encoding="utf-8")
    result = run_vitis_hls(script, work_dir=tmp_path)
    assert "WAVEFLOW_CSYNTH_OK" in result.stdout.splitlines()
    report_dir = tmp_path / f"{top}_proj" / "solution1" / "syn" / "report"
    target = synth_target(report_dir)
    assert target is not None
    assert target["part"].replace("-", "").lower() == board.part.replace("-", "").lower()
    assert target["target_period_ns"] == pytest.approx(10)
    assert cfg.clk_freq == 100e6 != clock.actual_hz
    assert any((report_dir.parent / "verilog").glob("*.v"))
