"""tests/calib/test_platform.py — the per-platform calibration library layout.

PlatformCalib is the directory a platform's two reusable calibrations share: the bus law (delegated to
BusCalib) and each infra component's residual, keyed by component identity under ``components/``.  This
checks the layout resolves as documented and that the bus accessor roots at the same directory.
"""
from __future__ import annotations

from pathlib import Path

from waveflow.calib.bus_model import BUS_MODEL_FILE, BusCalib
from waveflow.calib.platform import COMPONENTS_SUBDIR, PlatformCalib


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
