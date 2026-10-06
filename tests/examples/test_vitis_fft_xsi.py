"""S3 of ``plans/vitis_l1_hwmodule.md``: ``VitisFft`` at RTL, under XSI, and the pysim against it.

The free-running ``ap_ctrl_none`` top that ``composite_top_spec`` derives from the module itself --
four AXI-Stream lanes in, four out, the vendor's ``xf::dsp::fft::fft<>`` inside an ``hls::task`` --
driven by the BFM harness that ``tb_top_spec`` derives from the testbench graph, for the RFSoC 4x2
at 250 MHz, at ``L = 16``.

Two scenarios, both traced and read at the ports (``TVALID && TREADY``), so the RTL and the pysim
share a time origin -- frame 0's first input beat -- with no offset constant in between:

* **back to back**: four frames queued at once;
* **isolated frames**: seeded gaps that sweep arrival phase, the case where the vendor core's
  free-running input commutator makes the processing delay depend on when a frame arrives.

Gates: bits exact in both (inside every run); the back-to-back frame times exact; the pysim, timed
from the platform (``waveflow/calib/platforms/rfsoc4x2_bfm_250mhz``), within the platform's measured
spread; and the platform's committed RTL measurements still describing this RTL.

Run: ``pytest tests/examples/test_vitis_fft_xsi.py -m xsi`` (needs Vivado, and
``python -m examples.vitis_fft.vitis_fft_build`` for the csynth'd top).
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pandas as pd
import pytest

from examples.vitis_fft.vitis_fft import L
from examples.vitis_fft.vitis_fft_build import HERE as ROOT
from waveflow.build.trace_steps import rtl_staleness
from waveflow.calib.fixtures.vitis_fft import VitisFftFixture
from waveflow.simulation.simulation import Simulation
from waveflow.toolchain.toolchain import find_vivado_path
from waveflow.vitis_l1 import rtl
from waveflow.vitis_l1 import timing as vt
from waveflow.vitis_l1.testbench import VitisFftTB, default_platform_dir, pysim_frame_cycles, write_scenario

N_B2B, N_GAP = 4, 8
#: Back to back, each frame's last output beat, in cycles from frame 0's first input beat.  Recorded
#: 2026-10-05 on the RFSoC 4x2 target; a change is a real behaviour change -- look at it.
EXPECTED_B2B_DONE = [43, 84, 125, 166]


def _pysim_done(n_frames: int, gaps=()) -> list[int]:
    with tempfile.TemporaryDirectory() as d:
        write_scenario(d, n_frames, L)
        tb = VitisFftTB(name="tb", sim=Simulation(), length=L, n_frames=n_frames,
                        burst_gaps=list(gaps), root=Path(d))
        tb.sim.run_sim()
        return [round(c) for c in pysim_frame_cycles(tb)]


@pytest.fixture(scope="module")
def runs():
    if not find_vivado_path():
        pytest.skip("XSI gate prerequisite missing: Vivado (xvlog/xelab/xsim)")
    # include/, gen/ and xsi/ are untracked build output: regenerate (Python only) so the staleness
    # check compares the RTL against THIS checkout's sources (plans/source_layout.md).
    rtl.generate(ROOT, L)
    if not (ROOT / f"{rtl.TOP}_proj").is_dir() or not (ROOT / rtl.XSI_DIR / f"rtl_{rtl.TOP}.f").exists():
        pytest.skip("XSI gate prerequisite missing: no csynth RTL -- run "
                    "python -m examples.vitis_fft.vitis_fft_build")
    stale = rtl_staleness(ROOT, rtl.TOP)
    if stale is not None:
        pytest.skip(f"XSI gate prerequisite missing: {stale}")
    gaps = rtl.sweep_gaps(L, N_GAP)
    return {"b2b": rtl.run_scenario(ROOT, L, N_B2B), "gap": rtl.run_scenario(ROOT, L, N_GAP, gaps),
            "gaps": gaps}


@pytest.mark.xsi
def test_vitis_fft_rtl_back_to_back_cycles(runs):
    """Bits are checked inside every run; this pins the timing exactly."""
    assert [f["done"] for f in runs["b2b"]] == EXPECTED_B2B_DONE


@pytest.mark.xsi
def test_vitis_fft_pysim_within_the_measured_spread(runs):
    """The pysim, timed from the platform, against both scenarios.

    Back to back the core stays in step with its commutator, so the interval is exact.  An isolated
    frame's processing delay depends on its arrival phase, which an LT model cannot know: the pysim
    uses the mean, and every frame must land within the platform's measured spread of it.  At
    ``L = 16`` that spread is zero, so this is exact here; at 64 and above it is not.
    """
    proc_m, _ = VitisFftFixture.models(default_platform_dir())
    lo, hi = vt.proc_spread(proc_m.calib_dir, L)
    for name, n, gaps in (("b2b", N_B2B, ()), ("gap", N_GAP, runs["gaps"])):
        rtl_done = [f["done"] for f in runs[name]]
        py = _pysim_done(n, gaps)
        if name == "b2b":
            assert py == rtl_done, f"back to back: pysim {py} vs RTL {rtl_done}"
        errs = [p - r for p, r in zip(py[1:], rtl_done[1:])]
        assert all(-hi - 0.5 <= e <= -lo + 0.5 for e in errs), (
            f"{name}: pysim {py} vs RTL {rtl_done}; errors {errs} outside the measured spread "
            f"[{-hi:+.1f}, {-lo:+.1f}]")


@pytest.mark.xsi
def test_vitis_fft_platform_measurements_are_current(runs):
    """The platform's committed RTL tables must describe THIS RTL.  They are produced by the
    fixture and committed; a body or toolchain change that moved the timing would otherwise leave
    every design timed for hardware that no longer exists."""
    proc_m, ii_m = VitisFftFixture.models(default_platform_dir())
    ii_rtl = pd.read_csv(ii_m.calib_dir / "rtl" / f"L{L}" / "firings.csv")["span"]
    b = runs["b2b"]
    now = [b[k]["done"] - b[k - 1]["done"] for k in range(2, len(b))]
    assert set(now) == set(ii_rtl), (
        f"platform interval at L={L} is {sorted(set(ii_rtl))}, the RTL now does {sorted(set(now))}: "
        f"recalibrate (python -m waveflow.calib.fixtures.vitis_fft --remeasure --work <dir>)")
    proc_rtl = pd.read_csv(proc_m.calib_dir / "rtl" / f"L{L}" / "firings.csv")["span"]
    g = runs["gap"]
    now_proc = {g[k]["done"] - g[k]["last_in"] for k in range(1, len(g))}
    assert now_proc <= set(proc_rtl), f"proc spans {now_proc} not in the platform's {set(proc_rtl)}"
