"""``examples/vitis_fft`` without a toolchain: the pysim golden and the generated build tree.

The RTL rung is ``test_vitis_fft_xsi.py`` (``-m xsi``); this keeps the example honest in the fast
suite -- the same testbench graph, the same scenario files, the same golden.
"""
from __future__ import annotations

import numpy as np

from examples.vitis_fft.vitis_fft import (
    II_CYCLES,
    LATENCY_CYCLES,
    N_FRAMES,
    golden,
    pysim_frame_cycles,
    pysim_output,
    run_pysim,
)
from examples.vitis_fft.vitis_fft_build import TOP, generate


def test_pysim_is_bit_exact_and_frames_leave_every_II(tmp_path):
    tb = run_pysim(tmp_path)
    for k, ((g_re, g_im), (w_re, w_im)) in enumerate(zip(pysim_output(tb), golden())):
        assert np.array_equal(g_re, w_re) and np.array_equal(g_im, w_im), f"frame {k}"
    cycles = [round(c) for c in pysim_frame_cycles(tb)]
    assert cycles == [LATENCY_CYCLES + k * II_CYCLES for k in range(N_FRAMES)]


def test_generate_writes_the_build_tree(tmp_path):
    """Everything before csynth is Python: the top, its tcl, the body, the XSI workspace, the
    scenario.  The top gives the 42-bit output ports their own width."""
    top = generate(tmp_path)
    assert top == TOP
    cpp = (tmp_path / "gen" / f"{TOP}.cpp").read_text(encoding="utf-8")
    assert "hls::stream<ap_uint<42> >& m_out_0" in cpp
    assert "ap_ctrl_none" in cpp
    assert (tmp_path / "gen" / f"{TOP}.tcl").exists()
    assert (tmp_path / "include" / "vitis_fft_task.h").exists()
    for name in (f"{TOP}_ports.h", f"{TOP}_tb_harness.h", f"{TOP}_bfm_tb.cpp", "run.bat"):
        assert (tmp_path / "xsi" / name).exists(), name
    assert (tmp_path / "xsi" / "vectors" / "s_in_3" / "words.bin").exists()
