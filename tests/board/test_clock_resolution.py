from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from waveflow.board import (
    Board, ReferenceClock, ClockProbeError, ClockProfile, ClockRequest, ClockRequestError, ClockSource,
)
from waveflow.board import clock as clock_module
from waveflow.build.build import BuildConfig
from waveflow.build.composite_gen import render_tcl, tcl_target
from waveflow.calib.platform import PlatformMismatchError


@pytest.fixture
def fake_vivado(monkeypatch):
    calls = []

    def run(script, work_dir, *, args, required_version):
        calls.append((Path(script), Path(work_dir), args, required_version))
        assert Path(script).is_file()
        requested = float(args[5])
        source, generator = args[3:5]
        if generator == "ps_pl0":
            actual = 115_384_598 if requested == 123_456_000 else 99_999_985
            parameters = {
                "clock_source": "IOPLL", "reference_hz": 33_333_330,
                "divisor0": 13 if requested == 123_456_000 else 15, "divisor1": 1,
            }
        else:
            actual = 245_760_000
            parameters = {
                "input_hz": 122_880_000, "multiplier": 9.75,
                "input_divider": 1, "output_divider": 4.875,
            }
        record = {
            "part": args[2], "board_part": args[1], "vivado_version": required_version,
            "source": source, "generator": generator, "requested_hz": requested,
            "actual_hz": actual, "pin_hz": actual, "parameters": parameters,
        }
        stdout = "WAVEFLOW_CLOCK_RESULT: " + json.dumps(record) + "\nWAVEFLOW_CLOCK_OK\n"
        Path(work_dir, "vivado.log").write_text(stdout)
        return subprocess.CompletedProcess(["vivado"], 0, stdout, "")

    monkeypatch.setattr(clock_module, "run_vivado", run)
    return calls


@pytest.mark.parametrize("field,value", [
    ("requested_hz", v) for v in (None, 0, -1, True, "100", float("nan"), float("inf"))
] + [
    ("hls_target_hz", v) for v in (0, -1, True, float("nan"), float("inf"))
] + [
    ("tolerance_ppm", v) for v in (-1, None, True, float("nan"), float("inf"))
])
def test_invalid_request(field, value):
    kwargs = {"source": "ps_pl0", "requested_hz": 100e6, field: value}
    with pytest.raises(ValueError):
        ClockRequest(**kwargs)


def test_sources_are_board_owned_and_immutable(fake_vivado):
    board = Board.rfsoc4x2()
    assert board.clock_source("pl_ref_mmcm") == ClockSource("mmcm", "pl_reference")
    with pytest.raises(TypeError):
        board.clock_sources["extra"] = ClockSource("ps_pl0", "ps_reference")
    with pytest.raises(ValueError, match="unknown clock source"):
        board.resolve_clock(ClockRequest("adc_reference", 100e6))
    with pytest.raises(ValueError, match="unknown reference"):
        replace(board, clock_sources={"bad": ClockSource("mmcm", "missing")})
    with pytest.raises(ValueError):
        ClockProfile(None, 100e6)
    assert not fake_vivado


def test_success_keeps_physical_and_hls_targets_separate(tmp_path, fake_vivado):
    board = Board.rfsoc4x2()
    request = ClockRequest("ps_pl0", 100e6, 100e6)
    clock = board.resolve_clock(request, work_dir=tmp_path)
    assert clock.profile == ClockProfile(99_999_985, 100e6)
    assert clock.error_ppm == pytest.approx(0.15)
    assert clock.accepted
    assert clock.validation_level == "ip_configuration"
    assert clock.work_dir.parent == tmp_path
    assert clock.log_path.is_file()
    manifest = json.loads(clock.manifest_path.read_text())
    assert manifest["accepted"] is True
    assert manifest["request"]["requested_hz"] == 100e6
    assert manifest["profile"]["physical_hz"] == 99_999_985
    assert manifest["context"]["board_file_hashes"]["board.xml"]
    assert fake_vivado[0][3] == "2025.1"
    with pytest.raises(TypeError):
        clock.generator_parameters["divisor0"] = 2
    cfg = BuildConfig(board=board, resolved_clock=clock)
    assert cfg.clk_freq == 100e6
    assert tcl_target(cfg) == (board.part, 10.0)
    assert "create_clock -period 10\n" in render_tcl("probe", part=cfg.part, period_ns=10)
    assert len(fake_vivado) == 1  # BuildConfig must not start another tool process.


def test_round_trip_and_unique_artifact_directories(tmp_path, fake_vivado):
    board = Board.rfsoc4x2()
    first = board.resolve_clock(ClockRequest("ps_pl0", 100e6, 100e6), work_dir=tmp_path)
    second = board.resolve_clock(
        ClockRequest("ps_pl0", first.actual_hz, 100e6), work_dir=tmp_path,
    )
    assert first.actual_hz == second.actual_hz
    assert second.error_ppm == 0
    assert first.work_dir != second.work_dir
    assert first.manifest_path.is_file()
    assert len(fake_vivado) == 2


@pytest.mark.parametrize("requested,tolerance", [(123.456e6, 100), (100e6, 0)])
def test_rejected_request_retains_candidate(tmp_path, fake_vivado, requested, tolerance):
    board = Board.rfsoc4x2()
    with pytest.raises(ClockRequestError, match="exceeds tolerance") as error:
        board.resolve_clock(ClockRequest("ps_pl0", requested, 100e6, tolerance), work_dir=tmp_path)
    candidate = error.value.resolution
    assert not candidate.accepted
    assert json.loads(candidate.manifest_path.read_text())["accepted"] is False
    with pytest.raises(ClockRequestError):
        BuildConfig(board=board, resolved_clock=candidate)


def test_config_rejects_conflicts_and_stale_context(tmp_path, fake_vivado):
    board = Board.rfsoc4x2()
    clock = board.resolve_clock(ClockRequest("ps_pl0", 100e6, 100e6), work_dir=tmp_path)
    for kwargs, match in [
        ({"resolved_clock": clock}, "requires a board"),
        ({"board": board, "resolved_clock": clock, "clock_request": ClockRequest("ps_pl0", 125e6, 125e6)}, "differs"),
        ({"board": board, "resolved_clock": clock, "clk_freq": clock.actual_hz}, "HLS target"),
        ({"board": board, "resolved_clock": clock, "part": "wrong"}, "does not match board"),
        ({"board": replace(board, vivado_version="2026.1"), "resolved_clock": clock}, "does not match board"),
        ({"board": replace(board, part="wrong"), "resolved_clock": clock}, "does not match board"),
    ]:
        with pytest.raises(ValueError, match=match):
            BuildConfig(**kwargs)


def test_physical_only_resolution_cannot_drive_hls(tmp_path, fake_vivado):
    board = Board.rfsoc4x2()
    clock = board.resolve_clock(ClockRequest("ps_pl0", 100e6), work_dir=tmp_path)
    assert clock.accepted
    with pytest.raises(ValueError, match="has no HLS target"):
        BuildConfig(board=board, resolved_clock=clock)


def test_mmcm_profile_and_reference_change(tmp_path, fake_vivado):
    board = Board.rfsoc4x2()
    clock = board.resolve_clock(ClockRequest("pl_ref_mmcm", 245.76e6, 250e6), work_dir=tmp_path)
    assert clock.profile == ClockProfile(245.76e6, 250e6)
    assert tcl_target(BuildConfig(board=board, resolved_clock=clock)) == (board.part, 4)
    changed = replace(board, reference_clocks={**board.reference_clocks, "pl_reference": ReferenceClock(125e6)})
    with pytest.raises(ValueError, match="does not match board"):
        BuildConfig(board=changed, resolved_clock=clock)


def test_calibration_uses_hls_not_physical_frequency(tmp_path, fake_vivado):
    board = Board.rfsoc4x2()
    clock = board.resolve_clock(ClockRequest("ps_pl0", 100e6, 100e6), work_dir=tmp_path)
    cfg = BuildConfig(board=board, resolved_clock=clock, platform="resolved_test", platforms_root=tmp_path)
    assert cfg.platform_info.clk_freq == 100e6
    BuildConfig(platform="wrong_clock", part=board.part, clk_freq=clock.actual_hz, platforms_root=tmp_path)
    with pytest.raises(PlatformMismatchError, match="cannot override a board target"):
        BuildConfig(board=board, resolved_clock=clock, platform="wrong_clock",
                    platforms_root=tmp_path, allow_platform_mismatch=True)


@pytest.mark.parametrize("mutation", [
    "missing_sentinel", "echoed_sentinel", "duplicate_result", "bad_json", "wrong_part",
    "wrong_request", "wrong_version", "wrong_source", "bad_pin", "nan_frequency", "no_parameters",
])
def test_bad_tool_outputs_are_not_profiles(tmp_path, fake_vivado, monkeypatch, mutation):
    original = clock_module.run_vivado

    def bad(*args, **kwargs):
        result = original(*args, **kwargs)
        prefix = "WAVEFLOW_CLOCK_RESULT: "
        payload = json.loads(result.stdout.splitlines()[0][len(prefix):])
        if mutation in {"missing_sentinel", "echoed_sentinel"}:
            result.stdout = result.stdout.replace("\nWAVEFLOW_CLOCK_OK", "\n# puts WAVEFLOW_CLOCK_OK" if mutation == "echoed_sentinel" else "")
        elif mutation == "duplicate_result":
            result.stdout += result.stdout.splitlines()[0] + "\n"
        elif mutation == "bad_json":
            result.stdout = prefix + "not json\nWAVEFLOW_CLOCK_OK\n"
        else:
            key, value = {
                "wrong_part": ("part", "wrong"), "wrong_request": ("requested_hz", 90e6),
                "wrong_version": ("vivado_version", "2026.1"), "wrong_source": ("source", "wrong"),
                "bad_pin": ("pin_hz", 80e6), "nan_frequency": ("actual_hz", float("nan")),
                "no_parameters": ("parameters", None),
            }[mutation]
            payload[key] = value
            result.stdout = prefix + json.dumps(payload) + "\nWAVEFLOW_CLOCK_OK\n"
        return result

    monkeypatch.setattr(clock_module, "run_vivado", bad)
    with pytest.raises(ClockProbeError):
        Board.rfsoc4x2().resolve_clock(ClockRequest("ps_pl0", 100e6, 100e6), work_dir=tmp_path)
    assert not list(tmp_path.glob("*/clock_resolution.json"))


def test_tool_failure_never_falls_back_to_declared_profile(tmp_path, monkeypatch):
    def missing(*args, **kwargs):
        raise RuntimeError("Vivado installation not found")

    monkeypatch.setattr(clock_module, "run_vivado", missing)
    with pytest.raises(ClockProbeError, match="Vivado installation not found"):
        Board.rfsoc4x2().resolve_clock(ClockRequest("ps_pl0", 100e6, 100e6), work_dir=tmp_path)



def test_modified_board_files_invalidate_resolution(tmp_path, fake_vivado):
    board = Board.rfsoc4x2()
    repo = tmp_path / "board_repo"
    shutil.copytree(board.board_repo_dir, repo)
    board = replace(board, board_repo_dir=repo)
    clock = board.resolve_clock(ClockRequest("ps_pl0", 100e6, 100e6), work_dir=tmp_path)
    preset = board.board_files_dir / "preset.xml"
    preset.write_text(preset.read_text() + "\n<!-- changed preset -->\n")
    with pytest.raises(ValueError, match="does not match board"):
        BuildConfig(board=board, resolved_clock=clock)
    assert len(fake_vivado) == 1


@pytest.mark.parametrize("generator,key,value", [
    ("ps_pl0", "divisor0", 0),
    ("ps_pl0", "divisor1", 1.5),
    ("ps_pl0", "clock_source", "unknown"),
    ("ps_pl0", "reference_hz", 40e6),
    ("mmcm", "input_hz", 125e6),
    ("mmcm", "multiplier", 10),
    ("mmcm", "output_divider", None),
])
def test_invalid_generator_parameters_are_rejected(
    tmp_path, fake_vivado, monkeypatch, generator, key, value,
):
    original = clock_module.run_vivado

    def bad(*args, **kwargs):
        result = original(*args, **kwargs)
        prefix = "WAVEFLOW_CLOCK_RESULT: "
        record = json.loads(result.stdout.splitlines()[0][len(prefix):])
        record["parameters"][key] = value
        result.stdout = prefix + json.dumps(record) + "\nWAVEFLOW_CLOCK_OK\n"
        return result

    monkeypatch.setattr(clock_module, "run_vivado", bad)
    source = "ps_pl0" if generator == "ps_pl0" else "pl_ref_mmcm"
    with pytest.raises(ClockProbeError):
        Board.rfsoc4x2().resolve_clock(ClockRequest(source, 100e6), work_dir=tmp_path)
    assert not list(tmp_path.glob("*/clock_resolution.json"))


def test_reference_change_invalidates_ps_resolution(tmp_path, fake_vivado):
    board = Board.rfsoc4x2()
    clock = board.resolve_clock(ClockRequest("ps_pl0", 100e6, 100e6), work_dir=tmp_path)
    changed = replace(board, reference_clocks={**board.reference_clocks,
                                             "ps_reference": ReferenceClock(40e6)})
    with pytest.raises(ValueError, match="does not match board"):
        BuildConfig(board=changed, resolved_clock=clock)


def test_clock_hardware_change_invalidates_resolution(tmp_path, fake_vivado):
    board = Board.rfsoc4x2()
    clock = board.resolve_clock(ClockRequest("ps_pl0", 100e6, 100e6), work_dir=tmp_path)
    devices = {**board.clock_devices,
               "si5395": replace(board.clock_devices["si5395"], model="different clock chip")}
    with pytest.raises(ValueError, match="does not match board"):
        clock.require_compatible(replace(board, clock_devices=devices))


def test_unachievable_clock_stops_before_hls(tmp_path, fake_vivado, monkeypatch):
    from waveflow.build import ip_export
    def forbidden(*args, **kwargs):
        pytest.fail("HLS must not launch for a rejected clock request")
    monkeypatch.setattr(ip_export, "run_vitis_hls", forbidden)
    source = tmp_path / "probe.cpp"
    source.write_text("unsigned probe(unsigned x) { return x + 1; }\n")
    config = BuildConfig(root_dir=tmp_path, board=Board.rfsoc4x2(),
                         clock_request=ClockRequest("ps_pl0", 123.456e6, 125e6))
    with pytest.raises(ClockRequestError):
        ip_export.synthesize_for_board(config, top="probe", inputs={"probe.cpp": source},
                                       sources=("probe.cpp",))
    assert not list(tmp_path.rglob("synthesis_receipt.json"))
