"""The ``add_state`` Stage-2 gate: cross-firing state survives in real RTL.

csynth (``tests/hw/test_add_state_vitis.py``) proves the emitted ``static`` compiles and infers a
memory.  It cannot prove the value **persists between firings** — an initialized static swept by
``ap_rst`` would csynth identically and then quietly zero itself every job.  ``plans/add_state.md``
flags exactly that as verify-empirically.  This is the check.

The discriminator is deliberately loud: five all-ones vectors through a per-lane running total give
``1,2,3,4,5`` per lane if the state persists and ``1,1,1,1,1`` if it does not.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from examples.state_toy.state_toy import (
    StateAccumTB,
    expected_words,
    input_words,
    write_scenario,
)
from examples.state_toy.state_toy_system_build import HERE, TOP, build_dag, check_xsi, system
from tests.examples._system_run import WORK, compared_clean, recorded_names, system_run
from waveflow.simulation.simulation import Simulation
NVEC = 5


def _flat(bursts) -> np.ndarray:
    return (np.concatenate([np.asarray(b).ravel() for b in bursts])
            if bursts else np.array([], dtype=np.uint64)).astype(np.uint64)


def test_pysim_accumulates_across_firings(tmp_path):
    """The Python golden: the running total is carried, not recomputed."""
    tb = StateAccumTB(name="tb", sim=Simulation(), nvec=NVEC)
    write_scenario(tmp_path, NVEC)
    tb.driver.root = tmp_path
    tb.sink.root = tmp_path
    tb.sim.run_sim()
    got = _flat(tb.sink.words)
    np.testing.assert_array_equal(got, expected_words(NVEC))


def test_scenario_is_all_ones():
    """Guard the discriminator itself: if the input stopped being all-ones, a design with NO state
    could still match the golden and the gate would go quiet."""
    for burst in input_words(NVEC):
        np.testing.assert_array_equal(np.asarray(burst), np.ones_like(np.asarray(burst)))


def test_the_system_runs_the_same_golden():
    """The system the RTL gate runs (:class:`StateAccumSystem`) reproduces the golden in pysim."""
    np.testing.assert_array_equal(system().run(), expected_words(NVEC))


@pytest.fixture(scope="module")
def rtl_run() -> Path:
    """One RTL run of the system, shared by the gates below."""
    return system_run(build_dag(work_dir=WORK), HERE, TOP)


@pytest.mark.xsi
def test_rtl_state_persists_across_firings(rtl_run):
    """THE gate: the RTL's per-lane totals climb 1..5 across five re-firings of the hls::task.

    Run through the system flow (``examples/state_toy/state_toy_system_build.py``): the accumulator
    is the DUT, the driver and the sink answer through their own models.  Needs a prior csynth
    (``state_accum.tcl``) and Vivado; a stale or missing csynth fails the run rather than passing.
    """
    check_xsi(rtl_run)


@pytest.mark.xsi
def test_rtl_recording_matches_pysim(rtl_run):
    """What the sink recorded at RTL is what it recorded in pysim, word for word."""
    assert compared_clean(rtl_run) and recorded_names(rtl_run) == ["m_out"]
