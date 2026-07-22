"""tests/examples/test_default_platform.py — the shipped reference platform reproduces RTL.

`waveflow/calib/platforms/zynq7020_bfm_100mhz/` is the in-package calibration library (built by
examples/mem_copy/calibrate_platform.py from the measured mem_copy numbers, shipped as package data).
This guards it: loading it — the bus law + the writer's control residual — must reproduce the measured
writer RTL period at both swept sizes.  A drift in the committed params (or a regression in the
load/predict path) fails here, toolchain-free.
"""
from __future__ import annotations

import numpy as np
import pytest

from examples.mem_copy.calibrate_platform import COMPONENT, RTL_SPAN, SWEEP
from examples.mem_copy.mem_copy import xsi_jobs
from examples.mem_copy.mem_copy_sim import MemCopySim
from waveflow.calib.platform import Platform, packaged_platforms_dir

#: The shipped, in-package reference library (resolved wherever the package is installed).
PLATFORMS_ROOT = packaged_platforms_dir()
NAME = "zynq7020_bfm_100mhz"


def _period(dut) -> float:
    ends = np.asarray([r["end"] for r in dut.wstream.firing_records]) / (1.0 / dut.wstream.clk.freq)
    d = np.diff(ends)
    return float(np.median(d[len(d) // 2:]))


@pytest.mark.skipif(PLATFORMS_ROOT is None or not (PLATFORMS_ROOT / NAME / "platform.json").exists(),
                    reason="reference platform not present")
class TestReferencePlatform:
    def test_manifest_records_the_part_and_clock(self):
        plat = Platform.resolve(PLATFORMS_ROOT, NAME)
        assert plat.part == "xc7z020clg484-1"
        assert plat.clk_freq == 100e6
        assert plat.synth_period_ns == 10.0

    @pytest.mark.parametrize("nw", list(SWEEP))
    def test_loading_it_reproduces_the_rtl_period(self, nw):
        plat = Platform.resolve(PLATFORMS_ROOT, NAME)
        comp = str(plat.component_dir(COMPONENT))
        dut = MemCopySim(jobs=xsi_jobs(nw, SWEEP[nw]), calib_dir=comp,
                         platform_dir=str(plat.dir)).run()
        assert _period(dut) == pytest.approx(RTL_SPAN[nw], rel=0.02), (
            f"committed platform no longer reproduces RTL at n={nw} — params drifted?")
