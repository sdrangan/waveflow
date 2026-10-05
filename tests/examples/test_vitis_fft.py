"""``examples/vitis_fft`` without a toolchain: the pysim, the calibration table, the build tree.

The RTL rung is ``test_vitis_fft_xsi.py`` (``-m xsi``); this keeps the example honest in the fast
suite -- the same testbench graph, the same scenario files, the same golden, the committed timing.
"""
from __future__ import annotations

import numpy as np

from examples.vitis_fft.vitis_fft import (
    N_FRAMES,
    golden,
    measured_timing,
    pysim_frame_cycles,
    pysim_output,
    run_pysim,
)
from examples.vitis_fft.vitis_fft_build import MEASURE_LENGTHS, TOP, generate, sweep_gaps


def test_pysim_is_bit_exact_and_frames_leave_every_II(tmp_path):
    tb = run_pysim(tmp_path)
    for k, ((g_re, g_im), (w_re, w_im)) in enumerate(zip(pysim_output(tb), golden())):
        assert np.array_equal(g_re, w_re) and np.array_equal(g_im, w_im), f"frame {k}"
    t = measured_timing(tb.length)
    assert (tb.latency_cycles, tb.ii_cycles) == (round(t["latency_mean"]), t["ii_cycles"]), \
        "the testbench must take its timing from the calibration table"
    cycles = [round(c) for c in pysim_frame_cycles(tb)]
    assert cycles == [tb.latency_cycles + k * tb.ii_cycles for k in range(N_FRAMES)]


def test_the_calibration_table_is_complete_and_sane():
    """Every calibrated length has an exact II and a mean latency inside its own measured range."""
    for length in MEASURE_LENGTHS:
        t = measured_timing(length)
        assert t is not None, f"L={length} is not calibrated: run vitis_fft_build --measure"
        assert t["ii_spread"][0] == t["ii_spread"][1] == t["ii_cycles"], "back to back is exact"
        assert t["latency_min"] <= t["latency_mean"] <= t["latency_max"]
        assert t["n_samples"] >= 32, "too few phases sampled to call the mean a mean"


def test_sweep_gaps_isolate_frames_and_spread_phase():
    """Each gap clears the frame interval measured at that length, and the extras vary over more
    than one commutator period (2.5 L/R), so the arrivals sample its phase."""
    for length in MEASURE_LENGTHS:
        g = np.array(sweep_gaps(length, 48))
        assert g.min() + length // 4 > measured_timing(length)["latency_max"]
        assert g.max() - g.min() > 2.5 * length / 4


def test_generate_writes_the_build_tree(tmp_path):
    """Everything before csynth is Python: the top, its tcl, the body, the XSI workspace, the
    scenario.  The top gives the 42-bit output ports their own width, for the RFSoC 4x2."""
    top = generate(tmp_path)
    assert top == TOP
    cpp = (tmp_path / "gen" / f"{TOP}.cpp").read_text(encoding="utf-8")
    assert "hls::stream<ap_uint<42> >& m_out_0" in cpp
    assert "ap_ctrl_none" in cpp
    tcl = (tmp_path / "gen" / f"{TOP}.tcl").read_text(encoding="utf-8")
    assert "xczu48dr" in tcl
    assert (tmp_path / "include" / "vitis_fft_task.h").exists()
    for name in (f"{TOP}_ports.h", f"{TOP}_tb_harness.h", f"{TOP}_bfm_tb.cpp", "run.bat",
                 f"vcd_dumper_{TOP}.v"):
        assert (tmp_path / "xsi" / name).exists(), name
    assert (tmp_path / "xsi" / "vectors" / "s_in_3" / "words.bin").exists()
