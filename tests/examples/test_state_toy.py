"""The ``add_state`` Stage-2 gate: cross-firing state survives in real RTL.

csynth (``tests/hw/test_add_state_vitis.py``) proves the emitted ``static`` compiles and infers a
memory.  It cannot prove the value **persists between firings** — an initialized static swept by
``ap_rst`` would csynth identically and then quietly zero itself every job.  ``plans/add_state.md``
flags exactly that as verify-empirically.  This is the check.

The discriminator is deliberately loud: five all-ones vectors through a per-lane running total give
``1,2,3,4,5`` per lane if the state persists and ``1,1,1,1,1`` if it does not.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import numpy as np
import pytest

from examples.state_toy.state_toy import (
    StateAccumTB,
    expected_words,
    input_words,
    write_scenario,
)
from waveflow.simulation.simulation import Simulation
from waveflow.utils.burst_io import read_burst_bundle

ROOT = Path(__file__).resolve().parents[2] / "examples" / "state_toy"
XSI = ROOT / "xsi"
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


@pytest.mark.xsi
def test_rtl_state_persists_across_firings():
    """THE gate: the RTL's per-lane totals climb 1..5 across five re-firings of the hls::task.

    Needs a prior ``vitis-run --mode hls --tcl state_accum.tcl`` and the XSI toolchain; skips
    loudly rather than passing when either is missing.
    """
    if os.name != "nt":
        pytest.skip("the XSI flow is a Windows .bat (xvlog/xelab/mingw)")
    if not (ROOT / "state_accum_proj").is_dir():
        pytest.skip(f"no csynth RTL at {ROOT / 'state_accum_proj'} — run state_accum.tcl first")
    if not (XSI / "run.bat").exists():
        pytest.skip(f"no XSI workspace at {XSI}")

    write_scenario(XSI, NVEC)
    out = XSI / "vectors" / "m_out"
    if out.exists():
        # A stale bundle from a previous run would let a broken build "pass" on old output.
        for f in out.iterdir():
            f.unlink()

    proc = subprocess.run(
        [str(XSI / "run.bat"), "state_accum", "state_accum_bfm_tb"],
        cwd=XSI, capture_output=True, text=True, shell=False,
    )
    assert proc.returncode == 0, f"XSI run failed\n{proc.stdout[-3000:]}\n{proc.stderr[-2000:]}"

    got = _flat(read_burst_bundle(out))
    want = expected_words(NVEC)
    assert got.size == want.size, (
        f"RTL produced {got.size} words, expected {want.size} — the run did not complete "
        f"(check the cycle bound)\n{proc.stdout[-2000:]}"
    )
    np.testing.assert_array_equal(got, want)
