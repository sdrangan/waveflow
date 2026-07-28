"""The B1 gate: a synthesis report attributed to the modules that caused it.

Two traps decide whether the numbers mean anything, and both are held down here.

**Hierarchy.**  The report is nested: ``fir_compute_serial_task_32_s`` reports DSP=32/LUT=3728 and its
child ``..._Pipeline_FIR`` reports DSP=32/LUT=2554 — the parent figure already *contains* the child.
Summing every row double-counts enormously.

**Completeness.**  A module with no matching row must raise.  Dropping one shrinks the per-module sum
and therefore inflates the integration term, which reads as "the modules are well modelled and the
glue is expensive" when the truth is "we lost a module" — the one direction of error this exercise
cannot tolerate.

The fixture is a *synthetic* parser so the gate runs without a toolchain; the numbers in it are the
real ones measured from ``examples/fir_block/fir_block_proj`` at ntap=32/samp_w=16/serial, so the
arithmetic asserted here is the arithmetic that actually held.
"""
from __future__ import annotations

import pytest

from waveflow.build.elaborate import elaborate
from waveflow.calib.module_key import walk_modules
from waveflow.calib.record_store import ModuleStore, normalize_resources
from waveflow.calib.synth_report import (
    SUBBLOCK_MARKER,
    UnmappedModuleError,
    attribute_resources,
    rtl_prefix,
    store_report,
)
from examples.fir_block.fir_block import MEM_DW, FirBlock

#: The measured fir_block report — ntap=32, samp_w=16, serial kernel, xc7z020.
_TOTAL = {"BRAM_18K": 2, "DSP": 32, "FF": 11347, "LUT": 8674, "URAM": 0}
_AVAIL = {"BRAM_18K": 280, "DSP": 220, "FF": 106400, "LUT": 53200, "URAM": 0}


def _row(lut, ff, dsp=0, bram=0):
    """A report row, including the AVAIL_/UTIL_ context columns Vitis really emits."""
    return {"LUT": lut, "FF": ff, "DSP": dsp, "BRAM_18K": bram, "URAM": 0,
            "AVAIL_LUT": 53200, "UTIL_LUT": "~0", "AVAIL_FF": 106400, "UTIL_FF": "~0",
            "AVAIL_DSP": 220, "UTIL_DSP": 0, "AVAIL_BRAM": 280, "UTIL_BRAM": 0}


_MODULE_INFO = {
    "entry_proc": _row(29, 2),
    "fir_cmd_rx_task_32_s": _row(279, 107),
    "mem_r_stream_framed_task_32_Pipeline_RELAY": _row(157, 34),
    "mem_r_stream_framed_task_32_Pipeline_A2S": _row(174, 70),
    "mem_r_stream_framed_task_32_s": _row(833, 472),
    "fir_compute_serial_task_32_Pipeline_FIR": _row(2554, 5368, dsp=32),
    "fir_compute_serial_task_32_Pipeline_LOAD": _row(167, 98),
    "fir_compute_serial_task_32_s": _row(3728, 7355, dsp=32),
    "mem_w_stream_framed_done_task_32_8_Pipeline_BUFF": _row(1054, 386),
    "mem_w_stream_framed_done_task_32_8_Pipeline_S2A": _row(139, 68),
    "mem_w_stream_framed_done_task_32_8_Pipeline_ECHO": _row(168, 66),
    "mem_w_stream_framed_done_task_32_8_s": _row(1850, 1464),
    "fir_block": _row(8676, 11347, dsp=32, bram=2),
}


class FakeParser:
    """Stands in for :class:`~waveflow.utils.csynthparse.CsynthParser` with measured numbers."""

    def __init__(self, total=None, avail=None, modules=None):
        self._t, self._a, self._m = total or _TOTAL, avail or _AVAIL, modules or _MODULE_INFO

    def get_total_resources(self):
        self.total_resources, self.available_resources = dict(self._t), dict(self._a)

    def get_module_resources(self):
        self.module_info = {k: dict(v) for k, v in self._m.items()}


def _top(**over):
    params = {"mem_dwidth": MEM_DW, "ntap": 32, "samp_w": 16, "samp_i": 2, "unroll_lane": False}
    params.update(over)
    return elaborate(FirBlock, params, name="fir_block")


@pytest.fixture()
def report():
    return attribute_resources(_top(), FakeParser(), top_name="fir_block")


# ---------------------------------------------------------------------------
# The hierarchy trap
# ---------------------------------------------------------------------------

def test_subblocks_are_breakdown_not_addends(report):
    """A ``_Pipeline_*`` row is already inside its parent's figure and must not be summed."""
    compute = next(m for m in report.modules if m.cls_name == "FirCompute")
    assert compute.resources["lut"] == 3728            # the parent row, not parent+children
    assert set(compute.subblocks) == {"fir_compute_serial_task_32_Pipeline_FIR",
                                      "fir_compute_serial_task_32_Pipeline_LOAD"}
    # Proof the trap is real: naive summation would nearly double this module.
    naive = compute.resources["lut"] + sum(
        normalize_resources(v)["lut"] for v in compute.subblocks.values())
    assert naive > compute.resources["lut"] * 1.7


def test_module_sum_stays_below_the_design_total(report):
    """The arithmetic sanity check the double-count would blow: parts cannot exceed the whole."""
    for counter in ("lut", "ff", "dsp"):
        assert report.module_sum[counter] <= report.top[counter]


def test_measured_arithmetic_holds(report):
    """The numbers actually measured on fir_block at ntap=32/samp_w=16/serial."""
    assert report.module_sum["lut"] == 279 + 833 + 3728 + 1850      # 6690
    assert report.top["lut"] == 8674
    assert report.integration["lut"] == 8674 - 6690                 # 1984


# ---------------------------------------------------------------------------
# Attribution and the three terms
# ---------------------------------------------------------------------------

def test_every_module_is_mapped(report):
    assert {m.cls_name for m in report.modules} == {
        "FirCmdRx", "MemRStream", "FirCompute", "MemWStream"}


def test_the_composite_top_is_not_a_module(report):
    """A composite has no task of its own — its row *is* the design total."""
    assert "FirBlock" not in {m.cls_name for m in report.modules}
    assert "fir_block" not in report.unclaimed


def test_dsp_is_perfectly_additive(report):
    """All 32 DSPs sit in the compute (one per tap) with nothing in the glue.

    The strongest form of the additive premise, and it holds exactly for this counter.
    """
    compute = next(m for m in report.modules if m.cls_name == "FirCompute")
    assert compute.resources["dsp"] == 32
    assert report.module_sum["dsp"] == report.top["dsp"] == 32
    assert report.integration["dsp"] == 0


def test_bram_lives_entirely_in_the_integration_term(report):
    """No module reports BRAM; the design's 2 are inter-task FIFOs.

    The tap storage was partitioned into LUT/FF instead — exactly the storage-mapping discontinuity
    the plan predicted, and a reminder that a per-module BRAM prior is not the whole story.
    """
    assert all(m.resources.get("bram", 0) == 0 for m in report.modules)
    assert report.top["bram"] == 2
    assert report.integration["bram"] == 2


def test_integration_is_a_material_share_of_lut(report):
    """~23% of LUT is glue — enough that Σ-modules alone would badly underestimate the design."""
    frac = report.integration["lut"] / report.top["lut"]
    assert 0.15 < frac < 0.30


def test_framework_overhead_is_unclaimed_not_attributed(report):
    """``entry_proc`` is the DATAFLOW entry process — real cost, but no module's."""
    assert "entry_proc" in report.unclaimed


# ---------------------------------------------------------------------------
# Completeness — a lost module must raise
# ---------------------------------------------------------------------------

def test_a_missing_module_row_raises(report):
    missing = {k: v for k, v in _MODULE_INFO.items() if not k.startswith("fir_compute")}
    with pytest.raises(UnmappedModuleError, match="fir_compute_serial_task_32"):
        attribute_resources(_top(), FakeParser(modules=missing), top_name="fir_block")


def test_the_error_names_what_was_available(report):
    missing = {k: v for k, v in _MODULE_INFO.items() if not k.startswith("fir_cmd_rx")}
    with pytest.raises(UnmappedModuleError, match="RTL rows present"):
        attribute_resources(_top(), FakeParser(modules=missing), top_name="fir_block")


# ---------------------------------------------------------------------------
# The name join
# ---------------------------------------------------------------------------

def test_rtl_prefix_is_derived_from_the_kernel_task():
    top = _top()
    memw = top.sub_comps["fir_block_memw"]
    assert rtl_prefix(memw.kernel_task()) == "mem_w_stream_framed_done_task_32_8"


def test_prefix_matching_does_not_confuse_a_longer_number():
    """``foo_32`` must not swallow ``foo_320`` — the shortest non-subblock match wins."""
    rows = dict(_MODULE_INFO)
    rows["fir_compute_serial_task_320_s"] = _row(9999, 9999)
    rep = attribute_resources(_top(), FakeParser(modules=rows), top_name="fir_block")
    compute = next(m for m in rep.modules if m.cls_name == "FirCompute")
    assert compute.rtl_module == "fir_compute_serial_task_32_s"
    assert compute.resources["lut"] == 3728


def test_subblock_marker_is_what_distinguishes_them():
    assert SUBBLOCK_MARKER in "fir_compute_serial_task_32_Pipeline_FIR"
    assert SUBBLOCK_MARKER not in "fir_compute_serial_task_32_s"


# ---------------------------------------------------------------------------
# Filing into the store
# ---------------------------------------------------------------------------

def test_records_land_under_each_module_key(tmp_path, report):
    store = ModuleStore(tmp_path / "platform")
    identities = {i.key: i for _, _, i in walk_modules(_top())}
    written = store_report(report, store, identities, source="hls_estimate",
                           part="xc7z020clg484-1", period_ns=10.0, cost_seconds=240.0)

    assert len(written) == 4
    assert len(store.keys()) == 4
    compute = next(m for m in report.modules if m.cls_name == "FirCompute")
    ident = identities[compute.key]
    rec = store.best(compute.key, "resource", identity=ident)
    assert rec.payload["dsp"] == 32
    assert rec.payload["rtl_module"] == "fir_compute_serial_task_32_s"
    assert rec.provenance.part == "xc7z020clg484-1"


def test_synthesis_cost_is_split_evenly_not_invented(tmp_path, report):
    """One indivisible run: guessing each module's share of it would be fabricating data."""
    store = ModuleStore(tmp_path / "platform")
    identities = {i.key: i for _, _, i in walk_modules(_top())}
    store_report(report, store, identities, cost_seconds=240.0)
    assert store.total_cost_seconds("resource") == pytest.approx(240.0)


def test_context_columns_never_reach_a_record(report):
    """``AVAIL_LUT`` summed across modules would look like a resource count and be nonsense."""
    for m in report.modules:
        assert not any(k.startswith(("avail_", "util_")) for k in m.resources)


def test_bram_18k_is_canonicalized(report):
    """Vitis spells it BRAM_18K; a record must not depend on which tool produced it."""
    assert "bram" in report.top and "bram_18k" not in report.top
