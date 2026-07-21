"""tests/calib/test_platform.py — the per-platform calibration library layout.

PlatformCalib is the directory a platform's two reusable calibrations share: the bus law (delegated to
BusCalib) and each infra component's residual, keyed by component identity under ``components/``.  This
checks the layout resolves as documented and that the bus accessor roots at the same directory.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from waveflow.calib.bus_model import BUS_MODEL_FILE, BusCalib
from waveflow.calib.platform import (
    COMPONENTS_SUBDIR,
    PLATFORM_MANIFEST,
    Platform,
    PlatformCalib,
    PlatformMismatchError,
    PlatformMismatchWarning,
)


class TestComponentDir:
    def test_keyed_by_component_under_components_subdir(self, tmp_path):
        pc = PlatformCalib(platform_dir=tmp_path)
        assert pc.component_dir("mem_w_stream_framed_done_task") == (
            tmp_path / COMPONENTS_SUBDIR / "mem_w_stream_framed_done_task")

    def test_two_components_get_distinct_dirs(self, tmp_path):
        pc = PlatformCalib(platform_dir=tmp_path)
        assert pc.component_dir("mem_r_stream_framed_task") != pc.component_dir(
            "mem_w_stream_framed_done_task")


class TestBusAccessor:
    def test_bus_roots_at_the_same_platform_dir(self, tmp_path):
        pc = PlatformCalib(platform_dir=tmp_path, clk_freq=200e6)
        bus = pc.bus
        assert isinstance(bus, BusCalib)
        assert Path(bus.platform_dir) == Path(tmp_path)
        assert bus.clk_freq == 200e6

    def test_bus_and_components_coexist_in_one_platform_dir(self, tmp_path):
        """The whole point of the library: fit the bus law and a component residual under ONE dir, so
        a project points at a single platform folder."""
        pc = PlatformCalib(platform_dir=tmp_path)
        pc.bus.fit(None, write_points=[
            {"num_trans": 8, "nwords": 128, "span": 142},
            {"num_trans": 32, "nwords": 512, "span": 574}])
        comp = pc.component_dir("mem_w_stream_framed_done_task")
        comp.mkdir(parents=True)
        (comp / "params.json").write_text("{}")

        assert (tmp_path / BUS_MODEL_FILE).exists()
        assert (tmp_path / COMPONENTS_SUBDIR / "mem_w_stream_framed_done_task" / "params.json").exists()


class TestResolve:
    def test_absent_platform_is_created_and_seeded(self, tmp_path):
        p = Platform.resolve(tmp_path, "zynq7020_bfm_100mhz",
                             part="xc7z020clg484-1", clk_freq=100e6)
        assert p.dir == tmp_path / "zynq7020_bfm_100mhz"
        assert p.part == "xc7z020clg484-1" and p.clk_freq == 100e6
        data = json.loads((p.dir / PLATFORM_MANIFEST).read_text())
        assert data == {"part": "xc7z020clg484-1", "clk_freq_hz": 100e6}
        assert p.synth_period_ns == 10.0                       # 100 MHz -> 10 ns HLS target

    def test_present_platform_confirms_and_adopts_stored_values(self, tmp_path):
        Platform.resolve(tmp_path, "plat", part="xc7z020clg484-1", clk_freq=100e6)
        # a second build selecting the same platform WITHOUT restating part/clk adopts the stored ones.
        p = Platform.resolve(tmp_path, "plat")
        assert p.part == "xc7z020clg484-1" and p.clk_freq == 100e6

    def test_part_mismatch_raises(self, tmp_path):
        Platform.resolve(tmp_path, "plat", part="xc7z020clg484-1", clk_freq=100e6)
        with pytest.raises(PlatformMismatchError, match="part"):
            Platform.resolve(tmp_path, "plat", part="xczu28dr-ffvg1517-2-e", clk_freq=100e6)

    def test_clock_mismatch_raises(self, tmp_path):
        Platform.resolve(tmp_path, "plat", part="xc7z020clg484-1", clk_freq=100e6)
        with pytest.raises(PlatformMismatchError, match="clk_freq"):
            Platform.resolve(tmp_path, "plat", part="xc7z020clg484-1", clk_freq=300e6)

    def test_allow_mismatch_warns_instead(self, tmp_path):
        Platform.resolve(tmp_path, "plat", part="xc7z020clg484-1", clk_freq=100e6)
        with pytest.warns(PlatformMismatchWarning, match="different target"):
            p = Platform.resolve(tmp_path, "plat", part="other", clk_freq=100e6,
                                 allow_mismatch=True)
        assert p.part == "xc7z020clg484-1"                     # stored value still wins
