"""tests/build/test_tcl_target.py — the csynth TCL pins the selected platform's part/clock.

render_tcl historically hardcoded xc7z020clg484-1 / period 10.  Now it takes part/period, driven from
the build's resolved platform via tcl_target — so the RTL a calibration measures is synthesised for the
exact part/clock the platform's fit is valid for.  The default must stay byte-identical (no platform =
no change to any existing example).
"""
from __future__ import annotations

from waveflow.build.build import BuildConfig
from waveflow.build.composite_gen import (
    DEFAULT_PART,
    DEFAULT_PERIOD_NS,
    render_tcl,
    tcl_target,
)


class TestTclTarget:
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

    def test_fractional_period_is_kept(self):
        tcl = render_tcl("t", period_ns=1e9 / 300e6)
        assert "create_clock -period 3.33" in tcl
