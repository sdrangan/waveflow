"""S3 of ``plans/vitis_l1_hwmodule.md``: ``VitisFft`` at RTL, under XSI, and the pysim against it.

The free-running ``ap_ctrl_none`` top that ``composite_top_spec`` derives from the module itself --
four AXI-Stream lanes in, four out, the vendor's ``xf::dsp::fft::fft<>`` inside an ``hls::task`` --
driven by the BFM harness that ``tb_top_spec`` derives from the testbench graph in
``examples/vitis_fft/vitis_fft.py``, for the RFSoC 4x2 at 250 MHz, at ``L = 16``.

Two scenarios, both traced and read at the ports (``TVALID && TREADY``), so the RTL and the pysim
share a time origin -- frame 0's first input beat -- with no offset constant in between:

* **back to back**: four frames queued at once; the interval is exact;
* **isolated frames**: seeded gaps that sweep arrival phase, the case where the vendor core's
  free-running input commutator makes latency depend on when a frame arrives.

Gates: bits exact in both; the back-to-back frame times exact; the pysim within the measured bound
(``examples/vitis_fft/measured/vitis_fft_timing.json``) in both; and that table still agreeing with
this RTL, so a stale calibration fails rather than quietly configuring the pysim.

Run: ``pytest tests/examples/test_vitis_fft_xsi.py -m xsi`` (needs Vivado, and
``python -m examples.vitis_fft.vitis_fft_build`` for the csynth'd top).
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from examples.vitis_fft import vitis_fft as ex
from examples.vitis_fft.vitis_fft_build import (
    HERE as ROOT,
    TOP,
    XSI_DIR,
    _check_bits,
    frame_times,
    generate,
    make_tb,
    port_beats,
    run_xsi,
    sweep_gaps,
    write_tb,
)
from waveflow.build.trace_steps import rtl_staleness
from waveflow.simulation.simulation import Simulation
from waveflow.toolchain.toolchain import find_vivado_path

L = ex.L
N_B2B, N_GAP = 4, 8
#: Back to back, each frame's last output beat, in cycles from frame 0's first input beat.  Recorded
#: 2026-10-05 on the RFSoC 4x2 target; a change is a real behaviour change -- look at it.
EXPECTED_B2B_DONE = [43, 84, 125, 166]


def _run(n_frames: int, gaps=()) -> list[dict]:
    write_tb(ROOT, make_tb(L, n_frames, gaps, untimed=True))
    run_xsi(ROOT, trace=True)
    _check_bits(ROOT, L, n_frames, "gate")
    return frame_times(port_beats(ROOT / XSI_DIR / f"{TOP}_trace.vcd"), L, n_frames)


def _pysim_done(n_frames: int, gaps=()) -> list[int]:
    with tempfile.TemporaryDirectory() as d:
        ex.write_scenario(d, n_frames, L)
        tb = ex.VitisFftTB(name="tb", sim=Simulation(), length=L, n_frames=n_frames,
                           burst_gaps=list(gaps), root=Path(d))
        tb.sim.run_sim()
        return [round(c) for c in ex.pysim_frame_cycles(tb)]


@pytest.fixture(scope="module")
def runs():
    if not find_vivado_path():
        pytest.skip("XSI gate prerequisite missing: Vivado (xvlog/xelab/xsim)")
    # include/, gen/ and xsi/ are untracked build output: regenerate (Python only) so the staleness
    # check compares the RTL against THIS checkout's sources (plans/source_layout.md).
    generate(ROOT)
    if not (ROOT / f"{TOP}_proj").is_dir() or not (ROOT / XSI_DIR / f"rtl_{TOP}.f").exists():
        pytest.skip("XSI gate prerequisite missing: no csynth RTL -- run "
                    "python -m examples.vitis_fft.vitis_fft_build")
    stale = rtl_staleness(ROOT, TOP)
    if stale is not None:
        pytest.skip(f"XSI gate prerequisite missing: {stale}")
    gaps = sweep_gaps(L, N_GAP)
    return {"b2b": _run(N_B2B), "gap": _run(N_GAP, gaps), "gaps": gaps}


@pytest.mark.xsi
def test_vitis_fft_rtl_back_to_back_cycles(runs):
    """Bits are checked inside every run; this pins the timing exactly."""
    assert [f["done"] for f in runs["b2b"]] == EXPECTED_B2B_DONE


@pytest.mark.xsi
def test_vitis_fft_pysim_within_the_measured_bound(runs):
    """The pysim, configured from the calibration table, against both scenarios.

    Back to back the core stays in step with its commutator, so the interval is exact.  An isolated
    frame waits for the commutator by an amount set by its arrival phase, which an LT model cannot
    know: it uses the mean latency, and every frame must land within the measured spread of it.  At
    ``L = 16`` that spread is zero, so this is exact here; at 64 and 256 it is not (see the table).
    """
    t = ex.measured_timing(L)
    lo, hi = t["latency_mean"] - t["latency_max"], t["latency_mean"] - t["latency_min"]
    for name, n, gaps in (("b2b", N_B2B, ()), ("gap", N_GAP, runs["gaps"])):
        rtl = [f["done"] for f in runs[name]]
        py = _pysim_done(n, gaps)
        if name == "b2b":
            assert [b - a for a, b in zip(py, py[1:])] == [b - a for a, b in zip(rtl, rtl[1:])], \
                f"intervals: pysim {py} vs RTL {rtl}"
        errs = [p - r for p, r in zip(py[1:], rtl[1:])]
        assert all(lo - 0.5 <= e <= hi + 0.5 for e in errs), (
            f"{name}: pysim {py} vs RTL {rtl}; errors {errs} outside the measured bound "
            f"[{lo:+.1f}, {hi:+.1f}]")


@pytest.mark.xsi
def test_vitis_fft_calibration_table_is_current(runs):
    """The table the pysim reads must describe THIS RTL.  It is produced by ``--measure`` and
    committed; a body or toolchain change that moved the timing would otherwise leave the pysim
    configured with numbers for hardware that no longer exists."""
    t = ex.measured_timing(L)
    rtl = [f["done"] for f in runs["b2b"]]
    assert t["b2b_done"][:N_B2B] == rtl, (
        f"measured/vitis_fft_timing.json says {t['b2b_done'][:N_B2B]} at L={L}, the RTL now does "
        f"{rtl}: rerun python -m examples.vitis_fft.vitis_fft_build --measure")
    assert t["ii_cycles"] == rtl[2] - rtl[1]
