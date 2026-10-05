"""``examples/vitis_fft`` without a toolchain: the pysim, the platform calibration, the build tree.

The RTL rung is ``test_vitis_fft_xsi.py`` (``-m xsi``); this keeps the example and ``VitisFft``'s
platform calibration honest in the fast suite -- the same testbench graph, the same scenario files,
the same golden, the committed platform tables.
"""
from __future__ import annotations

import numpy as np
import pytest

from examples.vitis_fft.vitis_fft import L, N_FRAMES, golden, pysim_frame_cycles, pysim_output, run_pysim
from examples.vitis_fft.vitis_fft_build import TOP, generate
from waveflow.calib.confidence import ConfidenceLevel
from waveflow.calib.fixtures.vitis_fft import LENGTHS, VitisFftFixture
from waveflow.simulation.simulation import Simulation
from waveflow.vitis_l1 import timing as vt
from waveflow.vitis_l1.rtl import sweep_gaps
from waveflow.vitis_l1.testbench import CLK_HZ, default_platform_dir

PL = L // 4                    # L/R: the cycles a frame's transfer takes on each channel


def _models():
    return VitisFftFixture.models(default_platform_dir())


def test_pysim_is_bit_exact_and_timed_from_the_platform(tmp_path):
    """At L=16 the platform says proc 35, interval 41.  The channels charge each transfer once, so
    the first frame finishes at ``L/R + 35 + L/R = 43`` and the rest every 41 -- the RTL's own
    numbers (``test_vitis_fft_xsi.py`` pins them)."""
    tb = run_pysim(tmp_path)
    for k, ((g_re, g_im), (w_re, w_im)) in enumerate(zip(pysim_output(tb), golden(N_FRAMES, L))):
        assert np.array_equal(g_re, w_re) and np.array_equal(g_im, w_im), f"frame {k}"
    proc_m, ii_m = _models()
    proc = proc_m.predict_feat(vt.features(L))[0]
    ii = ii_m.predict_feat(vt.features(L))[0]
    assert (round(proc), round(ii)) == (35, 41)
    cycles = [round(c) for c in pysim_frame_cycles(tb)]
    assert cycles == [PL + 35 + PL + k * 41 for k in range(N_FRAMES)] == [43, 84, 125, 166]


def test_every_shipped_length_is_calibrated_and_converged():
    """Each model has an entry at every length the fixture ships, and the pysim reproduced the RTL
    span there when it was fit (the fixture iterates the residual fit to a fixed point)."""
    for m in _models():
        df = m.gen_data_frame()
        assert sorted(df["L"].astype(int)) == sorted(LENGTHS), m.component
        assert (df["span_pysim"] - df["span_rtl"]).abs().max() < 0.5, m.component
        for n in LENGTHS:
            assert m.confidence_feat(vt.features(n)).level != ConfidenceLevel.UNCALIBRATED


def test_an_unmeasured_length_is_refused_not_extrapolated():
    """The timing is a lookup: past the 256 -> 1024 implementation change the obvious law misses by
    14-20%, so an unmeasured L must be calibrated, not guessed."""
    from waveflow.hw.clock import Clock
    from waveflow.vitis_l1.hw import VitisFft
    with pytest.raises(ValueError, match="no measurement at L=16384"):
        VitisFft(name="m", sim=Simulation(), clk=Clock(freq=CLK_HZ), L=16384,
                 platform_dir=default_platform_dir())


def test_sweep_gaps_isolate_frames_and_spread_phase():
    """Each gap clears the slowest isolated frame measured at that length, and the extras vary over
    more than one commutator period (2.5 L/R), so the arrivals sample its phase."""
    proc_m, _ = _models()
    for n in LENGTHS:
        spans = proc_m.gen_data_frame()
        worst = vt.proc_spread(proc_m.calib_dir, n)
        mean = float(spans.loc[spans["L"] == n, "span_rtl"].iloc[0])
        g = np.array(sweep_gaps(n, 48))
        assert g.min() > mean + worst[1] + n // 4, n
        assert g.max() - g.min() > 2.5 * n / 4, n


def test_generate_writes_the_build_tree(tmp_path):
    """Everything before csynth is Python: the top, its tcl, the body, the XSI workspace, the
    scenario.  The top gives the 42-bit output ports their own width, for the RFSoC 4x2."""
    top = generate(tmp_path)
    assert top == TOP
    cpp = (tmp_path / "gen" / f"{TOP}.cpp").read_text(encoding="utf-8")
    assert "hls::stream<ap_uint<42> >& m_out_0" in cpp
    assert "ap_ctrl_none" in cpp
    assert "xczu48dr" in (tmp_path / "gen" / f"{TOP}.tcl").read_text(encoding="utf-8")
    assert (tmp_path / "include" / "vitis_fft_task.h").exists()
    for name in (f"{TOP}_ports.h", f"{TOP}_tb_harness.h", f"{TOP}_bfm_tb.cpp", "run.bat",
                 f"vcd_dumper_{TOP}.v"):
        assert (tmp_path / "xsi" / name).exists(), name
    assert (tmp_path / "xsi" / "vectors" / "s_in_3" / "words.bin").exists()
