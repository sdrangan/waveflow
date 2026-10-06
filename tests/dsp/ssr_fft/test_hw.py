"""F3 of ``plans/ssr_fft.md``, the toolchain-free half: the composite's pysim and its build tree.

The RTL half is ``test_xsi.py`` (``-m xsi``).  Here: the composite ``SsrFft`` -- one pysim child per
``hls::task`` -- produces the golden bits through ``VitisFft``'s own testbench graph and scenario,
and everything before csynth (headers, wrappers, the generated top, the XSI workspace) is written by
Python alone.
"""
from __future__ import annotations

import numpy as np
import pytest

from waveflow.dsp.ssr_fft import hls
from waveflow.dsp.ssr_fft.hw import SsrFft
from waveflow.dsp.ssr_fft.model import Geometry
from waveflow.dsp.ssr_fft.testbench import golden, pysim_frame_cycles, pysim_output, run_pysim
from waveflow.simulation.simulation import Simulation


@pytest.mark.parametrize("reorder", ("sob", "pingpong"))
@pytest.mark.parametrize("length", (16, 64, 256))
def test_composite_pysim_is_bit_exact(tmp_path, length, reorder):
    tb = run_pysim(tmp_path, length=length, n_frames=3, reorder=reorder)
    out = pysim_output(tb)
    assert len(out) == 3
    for k, ((g_re, g_im), (w_re, w_im)) in enumerate(zip(out, golden(3, length))):
        assert np.array_equal(g_re, w_re) and np.array_equal(g_im, w_im), f"frame {k}"


@pytest.mark.parametrize("reorder,interval", (("sob", 19), ("pingpong", 16)))
def test_composite_pysim_streams_frames_back_to_back(tmp_path, reorder, interval):
    """Frames leave one interval apart: the chain overlaps them.  With the ping-pong reorder the
    interval is L/R exactly; the SOB pair adds its reader's per-frame block handover."""
    tb = run_pysim(tmp_path, length=64, n_frames=5, reorder=reorder)
    gaps = np.diff(pysim_frame_cycles(tb))
    assert np.allclose(gaps, interval), gaps


def test_one_child_per_task_with_its_wrapper():
    dut = SsrFft(name="f", sim=Simulation(), L=1024, timed=False)
    geo = Geometry(1024)
    names = [t.inst for t in hls.task_instances(geo)]
    assert names == (["lanes_in", "tp0", "tp1", "tp2", "tp3"]
                     + [x for s in range(5) for x in ((f"st{s}", f"cm{s}") if s < 4 else ("st4",))]
                     + ["rc", "rw", "rr", "lanes_out"])
    assert [c.kernel_task().task_fn for c in dut.tasks] == [
        f"ssr_fft_L1024_16_2_18_2_{n}" for n in names]
    assert dut.out_fmt.W == 27 and dut.s_in[0].bitwidth == 32 and dut.m_out[0].bitwidth == 54


def test_generate_writes_the_build_tree(tmp_path):
    from waveflow.dsp.ssr_fft.rtl import TOP, generate
    assert generate(tmp_path, 64, n_frames=2) == TOP
    cpp = (tmp_path / "gen" / f"{TOP}.cpp").read_text(encoding="utf-8")
    assert "ap_ctrl_none" in cpp and "hls::stream_of_blocks<ap_uint<184>[16], 2> blk;" in cpp
    assert cpp.count("hls_thread_local hls::task ") == 12
    assert "config_rtl -reset state" in (tmp_path / "gen" / f"{TOP}.tcl").read_text(encoding="utf-8")
    inc = tmp_path / "include"
    for name in ("ssr_fft_tasks.h", "ssr_fft_L64_16_2_18_2.h", "ssr_fft_L64_16_2_18_2_tasks.h",
                 "complex__fixed16_2_array_utils.h", "complex__fixed23_9_array_utils.h"):
        assert (inc / name).exists(), name
    for name in (f"{TOP}_ports.h", f"{TOP}_tb_harness.h", f"{TOP}_bfm_tb.cpp"):
        assert (tmp_path / "xsi" / name).exists(), name
