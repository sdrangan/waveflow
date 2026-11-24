from __future__ import annotations

import hashlib
from dataclasses import replace
from xml.etree import ElementTree

import pytest

from waveflow.board import Board, ReferenceClock, ClockDevice, ClockProfile, ClockRequest
from waveflow.build.build import BuildConfig


EXPECTED_BOARD_FILE_HASHES = {
    "LICENSE": "97c4412f28d79e45efb3899bdf46aba77e02c1eba793af974ff5cd78b9c944ec",
    "board.xml": "3213dec9df23775b655d2931de5ff33ffe3936e495f4156d6def3fd84c5fa658",
    "part0_pins.xml": "7625d0602a60693f5ec16b0e233f89410611f101a8211dd05affa8282bf6f6cf",
    "preset.xml": "bccc431bb0b525936a9e59b83f98d108d66bcfd2e945048171481a213d4380e3",
    "rfsoc4x2_board.png": "c77f9f8e4cdfd22c8f7c300e59802715b1f73924ee3183bfdd5dd17a7ee3ca3c",
    "xitem.json": "129056fee98c61761d7414250934f6e0105541ac7c14719a84e18f47d34dd005",
}


def test_rfsoc4x2_identity_and_reference_clocks():
    board = Board.rfsoc4x2()
    assert board.name == "rfsoc4x2"
    assert board.part == "xczu48dr-ffvg1517-2-e"
    assert board.board_part == "realdigital.org:rfsoc4x2:part0:1.0"
    assert board.board_files_version == "1.0"
    assert board.vivado_version == "2025.1"
    assert board.reference_clock("ps_reference").frequency_hz == 33_333_330
    assert board.reference_clock("pl_reference").frequency_hz == 122.88e6
    assert board.reference_clock("adc_reference").frequency_hz == 491.52e6
    assert board.reference_clock("dac_reference").frequency_hz == 491.52e6


def test_clock_inventory_matches_pinned_ps_selectors():
    board = Board.rfsoc4x2()
    parameters = {
        node.attrib["name"]: node.attrib["value"]
        for node in ElementTree.parse(board.board_files_dir / "preset.xml").iter("user_parameter")
    }
    ref_hz = float(parameters["CONFIG.PSU__PSS_REF_CLK__FREQMHZ"]) * 1e6
    assert board.reference_clock("ps_reference").frequency_hz == pytest.approx(ref_hz, abs=0.001, rel=0)
    for pll, domain in (("APLL", "CRF"), ("DPLL", "CRF"), ("VPLL", "CRF"),
                        ("IOPLL", "CRL"), ("RPLL", "CRL")):
        assert parameters[f"CONFIG.PSU__{domain}_APB__{pll}_CTRL__SRCSEL"] == "PSS_REF_CLK"
        assert board.clock_devices[f"ps_{pll.lower()}"].reference_clocks == ("ps_reference",)
    for field, consumer in (("CRL_APB__PL0_REF_CTRL", "PS PL0"),
                            ("CRL_APB__CPU_R5_CTRL", "PS CPU_R5"),
                            ("CRF_APB__DDR_CTRL", "PS DDR controller"),
                            ("CRF_APB__ACPU_CTRL", "PS ACPU")):
        pll = parameters[f"CONFIG.PSU__{field}__SRCSEL"].lower()
        assert consumer in board.clock_devices[f"ps_{pll}"].consumers
    # DDR's 1200 MHz is a derived preset setting, never an external reference.
    assert parameters["CONFIG.PSU__CRF_APB__DDR_CTRL__FREQMHZ"] == "1200"
    assert "ps_ddr" not in board.reference_clocks
    assert all(ref.frequency_hz != 1200e6 for ref in board.reference_clocks.values())


def test_external_clock_paths_and_programming_requirements():
    board = Board.rfsoc4x2()
    for name, hz, source, output in (
        ("pl_system_reference", 100e6, "si5395", "OUT0A"),
        ("pl_ddr_reference", 200e6, "si5395", "OUT1"),
        ("pl_reference", 122.88e6, "lmk", "DCLKout8"),
        ("adc_synth_reference", 245.76e6, "lmk", "DCLKout0"),
        ("dac_synth_reference", 245.76e6, "lmk", "DCLKout4"),
        ("adc_reference", 491.52e6, "lmx_adc", "RFoutA/B"),
        ("dac_reference", 491.52e6, "lmx_dac", "RFoutA/B"),
    ):
        ref = board.reference_clock(name)
        assert (ref.frequency_hz, ref.source, ref.output) == (hz, source, output)
    assert board.clock_devices["si5395"].configuration == "factory_rom"
    assert board.clock_devices["lmk"].reference_clocks == ("lmk_reference", "lmk_vcxo")
    for name in ("lmk", "lmx_adc", "lmx_dac"):
        assert board.clock_devices[name].configuration == "runtime_spi"
    assert board.clock_devices["lmx_adc"].reference_clocks == ("adc_synth_reference",)
    assert board.clock_devices["lmx_dac"].reference_clocks == ("dac_synth_reference",)
    # Inventory entries do not silently create generator implementations.
    assert set(board.clock_sources) == {"ps_pl0", "pl_ref_mmcm"}
    with pytest.raises(ValueError, match="unknown clock source"):
        board.clock_source("lmx_adc")


def test_inventory_is_immutable_and_rejects_broken_connections():
    board = Board.rfsoc4x2()
    with pytest.raises(TypeError):
        board.clock_devices["new"] = ClockDevice("new")
    with pytest.raises(ValueError, match="unknown reference clock"):
        replace(board, clock_devices={**board.clock_devices,
                                      "new": ClockDevice("new", ("missing",))})
    with pytest.raises(ValueError, match="unknown clock device"):
        replace(board, reference_clocks={**board.reference_clocks,
                                        "new": ReferenceClock(1e6, source="missing")})
    with pytest.raises(ValueError, match="configuration method"):
        ClockDevice("new", configuration="unimplemented")


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
        board.reference_clocks["extra"] = ReferenceClock(1e6)  # type: ignore[index]
    with pytest.raises(ValueError, match="unknown reference clock.*ps_reference"):
        board.reference_clock("missing")


@pytest.mark.parametrize("physical,hls", [(0, 1), (-1, 1), (1, 0), (float("inf"), 1)])
def test_clock_profile_rejects_invalid_frequencies(physical, hls):
    with pytest.raises(ValueError, match="positive finite"):
        ClockProfile(physical, hls)


def test_build_config_derives_board_part_and_hls_clock(tmp_path):
    board = Board.rfsoc4x2()
    cfg = BuildConfig(root_dir=tmp_path, board=board,
                      clock_request=ClockRequest("pl_ref_mmcm", 245.76e6, 250e6))
    assert cfg.part == board.part
    assert cfg.clk_freq == 250e6
    assert cfg.clock_request.requested_hz == 245.76e6
    assert cfg.resolved_clock is None


def test_board_target_is_applied_before_platform_resolution(tmp_path):
    board = Board.rfsoc4x2()
    cfg = BuildConfig(
        root_dir=tmp_path,
        board=board,
        clock_request=ClockRequest("ps_pl0", 100e6, 100e6),
        platform="rfsoc4x2_ps_control",
        platforms_root=tmp_path / "platforms",
    )
    assert cfg.platform_info.part == board.part
    assert cfg.platform_info.clk_freq == 100e6


@pytest.mark.parametrize(
    "kwargs,match",
    [
        ({"clock_request": ClockRequest("ps_pl0", 100e6, 100e6)}, "requires a board"),
        ({"board": Board.rfsoc4x2()}, "requires clock_request"),
        (
            {"board": Board.rfsoc4x2(), "clock_request": ClockRequest("ps_pl0", 100e6, 100e6), "part": "wrong"},
            "does not match board",
        ),
        (
            {"board": Board.rfsoc4x2(), "clock_request": ClockRequest("ps_pl0", 100e6, 100e6), "clk_freq": 125e6},
            "does not match clock request",
        ),
        (
            {"board": Board.rfsoc4x2(), "clock_request": ClockRequest("ps_pl0", 100e6)},
            "has no HLS target",
        ),
    ],
)
def test_build_config_rejects_incomplete_or_mismatched_board_target(tmp_path, kwargs, match):
    with pytest.raises(ValueError, match=match):
        BuildConfig(root_dir=tmp_path, **kwargs)


def test_board_rejects_application_output_as_reference():
    from dataclasses import replace
    with pytest.raises(TypeError, match="ReferenceClock"):
        replace(Board.rfsoc4x2(), reference_clocks={"application": ClockProfile(100e6, 100e6)})
