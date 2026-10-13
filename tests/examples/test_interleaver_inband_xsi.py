"""The in-band interleaver through real RTL for twelve jobs -- the m_axi pointer-FIFO regression gate.

**Why twelve.**  The interleaver reads twice per job (``P``, then ``X``) and writes once.  When the
generated top passed its ``m_axi`` pointers ``offset=slave``, Vitis gave each pointer-owning task a
small FIFO of its own, refilled in lockstep by one ``entry_proc``, popped once per firing.  The reader
drained its FIFO twice as fast as the writer; the writer's (depth 7) filled, the entry process
blocked, and the pipeline stopped after **6** jobs, every bus idle -- bit-exact until then, so a
one- or two-job test (all the interleaver's RTL tests had) could never see it.  The generator now
emits ``offset=off`` + ``stable`` (UG1399's supported form for a pointer on an ``hls::task``), which
removes the entry process and the FIFOs.  This gate keeps it that way: an imbalance is built into the
design, and the scenario runs well past the old wall.  See ``plans/maxi_pointer_fifo.md``.

Needs a prior csynth (:func:`examples.interleaver.interleaver_inband.build_xsi_gate_rtl`) plus the XSI
toolchain; skips loudly rather than passing when either is missing.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from examples.interleaver.interleaver_inband_system_build import (HERE, TOP, WANT_LAST_DONE_CYCLE,
                                                                  build_dag, check_xsi, system)
from tests.examples._system_run import WORK, compared_clean, recorded_names, system_run


def test_the_system_runs_the_same_golden():
    """The system the RTL gate runs passes the golden check in pysim."""
    system().run()


@pytest.fixture(scope="module")
def rtl_run() -> Path:
    """One RTL run of the twelve-job system, through
    ``examples/interleaver/interleaver_inband_system_build.py``: the kernel is the DUT; the driver,
    the sink and the memory answer through their own models."""
    return system_run(build_dag(work_dir=WORK), HERE, TOP)


@pytest.mark.xsi
def test_twelve_jobs_past_the_pointer_fifo_wall(rtl_run):
    """Twelve jobs, every Y region bit-exact, one completion each, the last at an exact cycle
    (:data:`~examples.interleaver.interleaver_inband_system_build.WANT_LAST_DONE_CYCLE`: 98 cycles a
    job at n=64 after a 222-cycle first job; before the fix the sixth was already 26 cycles late --
    the FIFO throttling -- and there was no seventh).  Fewer than twelve completions is the m_axi
    pointer-FIFO deadlock: did the generated top's m_axi go back to offset=slave?"""
    assert check_xsi(rtl_run) == WANT_LAST_DONE_CYCLE


@pytest.mark.xsi
def test_rtl_recordings_match_pysim(rtl_run):
    """The sink's completions and the memory's arena at RTL are pysim's, word for word."""
    assert compared_clean(rtl_run) and recorded_names(rtl_run) == ["out", "s_done"]
