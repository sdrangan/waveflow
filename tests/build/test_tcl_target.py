"""The csynth TCL pins the selected board or calibration platform's part/clock.

Board builds must reach the Tcl even without a calibration platform, using the HLS target instead
of the physical clock. Without a board, existing platform resolution and historical defaults remain
unchanged.
"""
from __future__ import annotations

import pytest

from waveflow.build.build import BuildConfig
from waveflow.build.composite_gen import (
    DEFAULT_PART,
    DEFAULT_PERIOD_NS,
    render_tcl,
    tcl_target,
)
from waveflow.board import Board, ClockRequest


class TestTclTarget:
    @pytest.mark.parametrize("clock_request,period_ns", [(ClockRequest("pl_ref_mmcm", 245.76e6, 250e6), 4.0),
                                                   (ClockRequest("ps_pl0", 100e6, 100e6), 10.0)])
    @pytest.mark.parametrize("with_platform", [False, True])
    def test_board_drives_generated_tcl(self, tmp_path, clock_request, period_ns, with_platform):
        cfg = BuildConfig(
            root_dir=tmp_path,
            board=Board.rfsoc4x2(),
            clock_request=clock_request,
            platform="board_test" if with_platform else None,
            platforms_root=tmp_path / "platforms",
        )
        part, period = tcl_target(cfg)
        assert (part, period) == ("xczu48dr-ffvg1517-2-e", period_ns)
        tcl = render_tcl("board_test", part=part, period_ns=period)
        assert "set part {xczu48dr-ffvg1517-2-e}" in tcl
        assert "set_part $part" in tcl
        assert f"create_clock -period {period_ns:g}\n" in tcl

    def test_board_target_survives_platform_without_target_metadata(self, tmp_path):
        # An older/incomplete calibration manifest must not send the board to legacy defaults.
        root = tmp_path / "platforms"
        BuildConfig(platform="unseeded", platforms_root=root)
        cfg = BuildConfig(
            board=Board.rfsoc4x2(), clock_request=ClockRequest("pl_ref_mmcm", 245.76e6, 250e6),
            platform="unseeded", platforms_root=root,
        )
        assert cfg.platform_info.part is None
        assert cfg.platform_info.clk_freq is None
        assert tcl_target(cfg) == ("xczu48dr-ffvg1517-2-e", 4.0)

    def test_no_platform_is_the_default(self, tmp_path):
        cfg = BuildConfig(root_dir=tmp_path)
        assert tcl_target(cfg) == (DEFAULT_PART, DEFAULT_PERIOD_NS)

    def test_none_config_is_the_default(self):
        assert tcl_target(None) == (DEFAULT_PART, DEFAULT_PERIOD_NS)

    def test_platform_drives_part_and_period(self, tmp_path):
        cfg = BuildConfig(root_dir=tmp_path, platform="rfsoc4x2_300mhz",
                          part="xczu28dr-ffvg1517-2-e", clk_freq=300e6,
                          platforms_root=tmp_path / "platforms")
        part, period = tcl_target(cfg)
        assert part == "xczu28dr-ffvg1517-2-e"
        assert period == 1e9 / 300e6                      # ~3.333 ns


class TestRenderTcl:
    def test_default_is_byte_identical_to_the_historical_tcl(self):
        """A regression pin: with no part/period args, the emitted TCL is exactly what it always was."""
        tcl = render_tcl("mem_copy")
        assert "set part {xc7z020clg484-1}" in tcl
        assert "create_clock -period 10" in tcl               # integer, not 10.0

    def test_override_emits_the_given_target(self):
        tcl = render_tcl("mem_copy", part="xczu28dr-ffvg1517-2-e", period_ns=5)
        assert "set part {xczu28dr-ffvg1517-2-e}" in tcl
        assert "create_clock -period 5" in tcl

    def test_src_is_searched_before_include(self):
        """A hand-written body in ``src/`` must win over a same-named leftover in ``include/``
        (``plans/source_layout.md``): the include order is what decides which one csynth reads."""
        assert 'set cf "-Isrc -Iinclude"' in render_tcl("mem_copy")

    def test_the_script_is_written_beside_its_top_in_gen(self, tmp_path):
        from waveflow.build.composite_gen import tcl_path
        assert tcl_path(tmp_path, "mem_copy") == tmp_path / "gen" / "mem_copy.tcl"

    def test_fractional_period_is_kept(self):
        tcl = render_tcl("t", period_ns=1e9 / 300e6)
        assert "create_clock -period 3.33" in tcl
