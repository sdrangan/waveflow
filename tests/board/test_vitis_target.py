"""Verify board-selected targets in fresh reports written by real Vitis HLS."""
from __future__ import annotations

import subprocess

import pytest

from waveflow.board import Board, ClockRequest
from waveflow.build.build import BuildConfig
from waveflow.build.composite_gen import render_tcl, tcl_target
from waveflow.toolchain import find_vitis_path, run_vitis_hls, tool_version
from waveflow.utils.csynthparse import synth_target


@pytest.fixture(scope="module")
def rfsoc_hls_board():
    exe = find_vitis_path()
    if exe is None:
        pytest.skip("Vitis HLS installation not found; cannot verify synthesized board target")
    board = Board.rfsoc4x2()
    assert tool_version(exe, "--version") == board.vivado_version
    return board


@pytest.mark.vitis
@pytest.mark.vivado
@pytest.mark.parametrize("clock_request,period_ns", [(ClockRequest("pl_ref_mmcm", 245.76e6, 250e6), 4.0),
                                               (ClockRequest("ps_pl0", 100e6, 100e6), 10.0)])
def test_board_target_in_fresh_csynth_report(tmp_path, rfsoc_hls_board, clock_request, period_ns):
    clock = rfsoc_hls_board.resolve_clock(clock_request, work_dir=tmp_path / "clocks")
    cfg = BuildConfig(root_dir=tmp_path, board=rfsoc_hls_board, resolved_clock=clock)
    top = "board_target_probe"
    (tmp_path / "gen").mkdir()
    (tmp_path / "include").mkdir()
    (tmp_path / "gen" / f"{top}.cpp").write_text(
        f"unsigned {top}(unsigned x) {{ return x + 1; }}\n", encoding="utf-8"
    )
    part, period = tcl_target(cfg)
    script = tmp_path / "run.tcl"
    script.write_text(render_tcl(top, part=part, period_ns=period), encoding="utf-8")
    try:
        result = run_vitis_hls(script, work_dir=tmp_path)
    except subprocess.CalledProcessError as exc:
        pytest.fail(f"Vitis synthesis failed:\n{exc.stdout}\n{exc.stderr}", pytrace=False)
    assert "WAVEFLOW_CSYNTH_OK" in result.stdout

    report_dir = tmp_path / f"{top}_proj" / "solution1" / "syn" / "report"
    actual = synth_target(report_dir)
    assert actual is not None, f"no target recorded in {report_dir / 'csynth.xml'}"
    # Vitis can canonicalize device names by adding hyphens.
    assert actual["part"].replace("-", "").lower() == "xczu48drffvg15172e"
    assert actual["target_period_ns"] == pytest.approx(period_ns)
    assert any((report_dir.parent / "verilog").glob("*.v"))
