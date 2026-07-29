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


class TestShippedResourceRecords:
    """The platform also carries **resource** measurements, so a design need not re-synthesize.

    ``waveflow/calib/platforms/zynq7020_bfm_100mhz/modules/`` holds the per-module figures published
    from the ``fir_block`` sweep — ~26 minutes of Vitis C-synthesis, distilled to 156 KB and shipped.
    Their whole purpose is to be a *cache hit*: a design that composes one of these modules at one of
    these configurations gets its area from the library instead of the toolchain.

    This guards that. It is toolchain-free by construction, which is the point — if it needed Vitis to
    verify, the records would not be doing their job.
    """

    @staticmethod
    def _store():
        from waveflow.calib.record_store import ModuleStore
        return ModuleStore(PLATFORMS_ROOT / NAME)

    def test_the_library_carries_module_records(self):
        store = self._store()
        assert len(store.keys()) >= 30, "the published module records are missing"
        by_class = {}
        for k in store.keys():
            by_class.setdefault(store.get_identity(k).cls_name, []).append(k)
        # The two memory modules are the reusable infra half: few configurations, broadly applicable.
        assert len(by_class["MemRStream"]) >= 1
        assert len(by_class["MemWStream"]) >= 1
        assert len(by_class["FirCompute"]) >= 24

    def test_a_lookup_hits_exactly_with_no_toolchain(self):
        """The cache-hit property, end to end: elaborate a design, get measured area back."""
        from waveflow.build.elaborate import elaborate
        from waveflow.calib.confidence import ConfidenceLevel
        from waveflow.calib.module_key import walk_modules
        from waveflow.calib.resource_model import LookupResourceModel
        from examples.fir_block.fir_block import FirBlock

        top = elaborate(FirBlock, {"mem_dwidth": 32, "ntap": 32, "samp_w": 16,
                                   "samp_i": 2, "unroll_lane": False}, name="fir_block")
        model = LookupResourceModel(store=self._store())
        for _, comp, ident in walk_modules(top):
            if ident.cls_name == "FirBlock":
                continue                       # the composite's own term is not a module record
            assert model.confidence_own(comp).level is ConfidenceLevel.EXACT, ident.cls_name
            assert model.predict_own(comp)["lut"] > 0

    def test_records_verify_against_the_module_they_describe(self):
        """Every stored record's provenance must match the identity it is filed under."""
        store = self._store()
        for key in store.keys():
            ident = store.get_identity(key)
            store.read(key, "resource", identity=ident)      # raises StaleRecordError on drift

    def test_the_cost_they_represent_is_recorded(self):
        """What the library cost to build, auditable from the library itself."""
        assert self._store().total_cost_seconds() > 20 * 60
