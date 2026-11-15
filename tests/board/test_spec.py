from __future__ import annotations

import hashlib

import pytest

from waveflow.board import Board, ClockProfile
from waveflow.build.build import BuildConfig


EXPECTED_BOARD_FILE_HASHES = {
    "LICENSE": "97c4412f28d79e45efb3899bdf46aba77e02c1eba793af974ff5cd78b9c944ec",
    "board.xml": "3213dec9df23775b655d2931de5ff33ffe3936e495f4156d6def3fd84c5fa658",
    "part0_pins.xml": "7625d0602a60693f5ec16b0e233f89410611f101a8211dd05affa8282bf6f6cf",
    "preset.xml": "bccc431bb0b525936a9e59b83f98d108d66bcfd2e945048171481a213d4380e3",
    "rfsoc4x2_board.png": "c77f9f8e4cdfd22c8f7c300e59802715b1f73924ee3183bfdd5dd17a7ee3ca3c",
    "xitem.json": "129056fee98c61761d7414250934f6e0105541ac7c14719a84e18f47d34dd005",
}


def test_rfsoc4x2_identity_and_clock_profiles():
    board = Board.rfsoc4x2()
    assert board.name == "rfsoc4x2"
    assert board.part == "xczu48dr-ffvg1517-2-e"
    assert board.board_part == "realdigital.org:rfsoc4x2:part0:1.0"
    assert board.board_files_version == "1.0"
    assert board.vivado_version == "2025.1"
    assert board.clock("ps_control") == ClockProfile(100e6, 100e6)
    assert board.clock("rf_axis") == ClockProfile(245.76e6, 250e6)
    assert board.clock("pl_reference") == ClockProfile(122.88e6, None)
    assert board.clock("adc_reference") == ClockProfile(491.52e6, None)
    assert board.clock("dac_reference") == ClockProfile(491.52e6, None)


def test_vendored_board_files_match_pinned_reference():
    root = Board.rfsoc4x2().board_files_dir
    actual = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.iterdir()
        if path.is_file()
    }
    assert actual == EXPECTED_BOARD_FILE_HASHES


def test_clock_map_is_immutable_and_unknown_name_is_clear():
    board = Board.rfsoc4x2()
    with pytest.raises(TypeError):
        board.clocks["extra"] = ClockProfile(1e6, 1e6)  # type: ignore[index]
    with pytest.raises(ValueError, match="unknown clock profile.*rf_axis"):
        board.clock("missing")


@pytest.mark.parametrize("physical,hls", [(0, 1), (-1, 1), (1, 0), (float("inf"), 1)])
def test_clock_profile_rejects_invalid_frequencies(physical, hls):
    with pytest.raises(ValueError, match="positive finite"):
        ClockProfile(physical, hls)


def test_build_config_derives_board_part_and_hls_clock(tmp_path):
    board = Board.rfsoc4x2()
    cfg = BuildConfig(root_dir=tmp_path, board=board, clock_profile="rf_axis")
    assert cfg.part == board.part
    assert cfg.clk_freq == 250e6
    assert board.clock("rf_axis").physical_hz == 245.76e6


def test_board_target_is_applied_before_platform_resolution(tmp_path):
    board = Board.rfsoc4x2()
    cfg = BuildConfig(
        root_dir=tmp_path,
        board=board,
        clock_profile="ps_control",
        platform="rfsoc4x2_ps_control",
        platforms_root=tmp_path / "platforms",
    )
    assert cfg.platform_info.part == board.part
    assert cfg.platform_info.clk_freq == 100e6


@pytest.mark.parametrize(
    "kwargs,match",
    [
        ({"clock_profile": "rf_axis"}, "requires a board"),
        ({"board": Board.rfsoc4x2()}, "requires clock_profile"),
        (
            {"board": Board.rfsoc4x2(), "clock_profile": "rf_axis", "part": "wrong"},
            "does not match board",
        ),
        (
            {"board": Board.rfsoc4x2(), "clock_profile": "rf_axis", "clk_freq": 245.76e6},
            "does not match clock profile",
        ),
        (
            {"board": Board.rfsoc4x2(), "clock_profile": "pl_reference"},
            "has no HLS target",
        ),
    ],
)
def test_build_config_rejects_incomplete_or_mismatched_board_target(tmp_path, kwargs, match):
    with pytest.raises(ValueError, match=match):
        BuildConfig(root_dir=tmp_path, **kwargs)
