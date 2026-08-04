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
    """The shipped library carries resource measurements for the **framework's own** modules.

    Their whole purpose is to be a *cache hit* for parts of a design you did not write: compose
    ``MemRStream`` on this part and its area comes from the library instead of the toolchain.

    What is guarded here is as much *what is absent* as what is present. A shared library must hold
    only ``waveflow.*`` modules — one project's own modules shipped to every installed user would be
    configurations nobody else can use, and would imply they are reference infrastructure when they
    are not. Example-specific records live in a project library beside the example.

    Toolchain-free by construction, which is the point: a guard needing Vitis would defeat the purpose
    of storing the numbers.
    """

    @staticmethod
    def _store():
        from waveflow.calib.record_store import ModuleStore
        return ModuleStore(PLATFORMS_ROOT / NAME)

    def test_the_library_carries_the_framework_modules(self):
        store = self._store()
        by_class = {}
        for k in store.keys():
            by_class.setdefault(store.get_identity(k).cls_name, []).append(k)
        assert len(by_class["MemRStream"]) >= 1
        assert len(by_class["MemWStream"]) >= 1

    def test_the_shared_library_holds_no_example_modules(self):
        """The dividing line, enforced: a shared library ships only what everyone can use.

        Not a judgement call — every record names its module's defining module, so "is this framework
        code or somebody's design?" is answerable mechanically.
        """
        store = self._store()
        stray = {k: store.get_identity(k).cls_module for k in store.keys()
                 if not store.get_identity(k).cls_module.startswith("waveflow.")}
        assert not stray, f"non-framework modules in the shipped library: {stray}"

    def test_a_framework_module_hits_exactly_with_no_toolchain(self):
        """The cache-hit property: a design composing MemRStream gets measured area, no toolchain."""
        from waveflow.build.elaborate import elaborate
        from waveflow.calib.confidence import ConfidenceLevel
        from waveflow.calib.module_key import walk_modules
        from waveflow.calib.resource_model import LookupResourceModel
        from examples.fir_block.fir_block import FirBlock

        top = elaborate(FirBlock, {"mem_dwidth": 32, "ntap": 32, "samp_w": 16,
                                   "samp_i": 2, "unroll_lane": False}, name="fir_block")
        model = LookupResourceModel(store=self._store())
        hit = 0
        for _, comp, ident in walk_modules(top):
            if not type(comp).__module__.startswith("waveflow."):
                continue                       # the example's own modules are not shipped
            assert model.confidence(comp).level is ConfidenceLevel.EXACT, ident.cls_name
            assert model.predict(comp)["lut"] > 0
            hit += 1
        assert hit == 2, "expected the two mem-streams"

    def test_records_verify_against_the_module_they_describe(self):
        """Every stored record's provenance must match the identity it is filed under."""
        store = self._store()
        for key in store.keys():
            ident = store.get_identity(key)
            store.read(key, "resource", identity=ident)      # raises StaleRecordError on drift

    def test_the_cost_they_represent_is_recorded(self):
        """What the library cost to build, auditable from the library itself."""
        assert self._store().total_cost_seconds() > 0
