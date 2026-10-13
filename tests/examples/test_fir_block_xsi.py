"""The block FIR's RTL gate: two flavours of declared state, bit-exact in real hardware simulation.

``examples/state_toy`` proved a ``static`` in a free-running ``hls::task`` body survives re-firing.
This proves the *design* built on that survives: coefficients held across firings, a per-block carry
that makes block-wise filtering equal filtering the whole signal, both dispatched from one compute
leaf, all through real RTL driven by the AXI-MM + AXI-Stream BFM.

**This gate earned its keep.**  csynth was clean and the first block matched on the very first run —
and the RTL was still wrong: seeding the delay line with the MAC-time invariant (``dline[k] = x[-k]``)
rather than the top-of-iteration one left the first ``SHIFT`` to slide it a slot, dropping the newest
carry sample.  Invisible to csynth, invisible in block 1 (which starts from zeros), visible only as
block 2's first samples.  Only a run against the golden could find it, which is the argument for
having the gate at all.

Needs a prior csynth (``fir_block.tcl``) plus the XSI toolchain; skips loudly rather than passing when
either is missing.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from examples.fir_block.fir_block_system_build import HERE, TOP, build_dag, check_xsi, system
from tests.examples._system_run import WORK, compared_clean, recorded_names, system_run


def test_the_system_runs_the_same_golden():
    """The system the RTL gate runs (:class:`~examples.fir_block.fir_block_sim.FirBlockSystem`)
    passes the golden check in pysim."""
    system().run()


@pytest.fixture(scope="module")
def rtl_run() -> Path:
    """One RTL run of the system, through ``examples/fir_block/fir_block_system_build.py``: the
    filter kernel is the DUT; the driver, the sink and the memory behind both ``m_axi`` bundles
    answer through their own models."""
    return system_run(build_dag(work_dir=WORK), HERE, TOP)


@pytest.mark.xsi
def test_rtl_matches_golden_across_reload_and_carry(rtl_run):
    """THE gate: LOAD_TAPS -> FILTER x2 -> LOAD_TAPS -> FILTER through real RTL, every output block
    bit-exact against the stateless golden, one completion per command."""
    check_xsi(rtl_run)


@pytest.mark.xsi
def test_rtl_recordings_match_pysim(rtl_run):
    """The sink's completions and the memory's arena at RTL are pysim's, word for word."""
    assert compared_clean(rtl_run) and recorded_names(rtl_run) == ["out", "s_done"]
