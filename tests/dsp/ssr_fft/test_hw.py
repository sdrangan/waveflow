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


@pytest.mark.parametrize("lanes", (False, True))
@pytest.mark.parametrize("reorder", ("sob", "pingpong"))
@pytest.mark.parametrize("length", (16, 64, 256))
def test_composite_pysim_is_bit_exact(tmp_path, length, reorder, lanes):
    tb = run_pysim(tmp_path, length=length, n_frames=3, reorder=reorder, lanes=lanes)
    out = pysim_output(tb)
    assert len(out) == 3
    for k, ((g_re, g_im), (w_re, w_im)) in enumerate(zip(out, golden(3, length))):
        assert np.array_equal(g_re, w_re) and np.array_equal(g_im, w_im), f"frame {k}"


@pytest.mark.parametrize("lanes", (False, True))
@pytest.mark.parametrize("length", (16, 64))
def test_inverse_composite_pysim_is_bit_exact(tmp_path, length, lanes):
    tb = run_pysim(tmp_path, length=length, n_frames=3, reorder="pingpong", lanes=lanes,
                   inverse=True)
    assert tb.dut.out_fmt.int_bits == 3
    out = pysim_output(tb)
    assert len(out) == 3
    for k, ((g_re, g_im), (w_re, w_im)) in enumerate(zip(out, golden(3, length, inverse=True))):
        assert np.array_equal(g_re, w_re) and np.array_equal(g_im, w_im), f"frame {k}"


def test_inverse_has_its_own_configuration():
    """Its own headers and wrappers (``_inv``): a forward and an inverse can share a design."""
    fwd, inv = Geometry(64), Geometry(64, inverse=True)
    assert hls.config_key(inv) == hls.config_key(fwd) + "_inv"
    assert hls.render_config(fwd) != hls.render_config(inv)


@pytest.mark.parametrize("reorder,interval", (("sob", 21), ("pingpong", 16)))
def test_composite_pysim_streams_frames_back_to_back(tmp_path, reorder, interval):
    """Frames leave one interval apart: the chain overlaps them.  With the ping-pong reorder the
    interval is L/R exactly; the SOB pair adds its reader's per-frame block handover."""
    tb = run_pysim(tmp_path, length=64, n_frames=5, reorder=reorder)
    gaps = np.diff(pysim_frame_cycles(tb))
    assert np.allclose(gaps, interval), gaps


@pytest.mark.parametrize("lanes", (False, True))
def test_one_child_per_task_with_its_wrapper(lanes):
    dut = SsrFft(name="f", sim=Simulation(), L=1024, timed=False, lanes=lanes)
    geo = Geometry(1024)
    names = [t.inst for t in hls.task_instances(geo, lanes=lanes)]
    core = (["tp0", "tp1", "tp2", "tp3"]
            + [x for s in range(5) for x in ((f"st{s}", f"cm{s}") if s < 4 else ("st4",))]
            + ["rc", "rw", "rr"])
    assert names == (["lanes_in", *core, "lanes_out"] if lanes else ["in_reg", *core])
    assert [c.kernel_task().task_fn for c in dut.tasks] == [
        f"ssr_fft_L1024_16_2_18_2_{n}" for n in names]
    assert dut.out_fmt.W == 27
    if lanes:          # VitisFft's port group: one sample a word
        assert dut.s_in[0].bitwidth == 32 and dut.m_out[0].bitwidth == 54
    else:              # one RadixWord a beat: R samples
        assert dut.s_in.bitwidth == 128 and dut.m_out.bitwidth == 216


def test_generate_writes_the_build_tree(tmp_path):
    from waveflow.dsp.ssr_fft.rtl import TOP, generate
    assert generate(tmp_path, 64, n_frames=2) == TOP
    cpp = (tmp_path / "gen" / f"{TOP}.cpp").read_text(encoding="utf-8")
    assert "ap_ctrl_none" in cpp and "hls::stream_of_blocks<ap_uint<184>[16], 2> blk;" in cpp
    assert "hls::stream<ap_uint<128> >& s_in" in cpp and "hls::stream<ap_uint<184> >& m_out" in cpp
    assert cpp.count("hls_thread_local hls::task ") == 11
    assert "config_rtl -reset state" in (tmp_path / "gen" / f"{TOP}.tcl").read_text(encoding="utf-8")
    inc = tmp_path / "include"
    for name in ("ssr_fft_tasks.h", "ssr_fft_L64_16_2_18_2.h", "ssr_fft_L64_16_2_18_2_tasks.h",
                 "complex__fixed16_2_array_utils.h", "complex__fixed23_9_array_utils.h"):
        assert (inc / name).exists(), name
    # The RTL run is the system flow's (rtl.run_xsi): generate records the scenario it builds.
    from waveflow.dsp.ssr_fft.rtl import SCENARIO_JSON
    assert (tmp_path / SCENARIO_JSON).is_file()


#: XSI, RFSoC 4x2 at 250 MHz, ping-pong reorder, 8 frames back to back (2026-10-06): the first
#: frame's last output beat, in cycles from its first input beat.  The interval was exactly L/R.
#: Keyed (L, lanes): the lane builds came first; the RadixWord-port builds are what
#: examples/ssr_fft/ssr_fft_measure.py records in measured.json (tests/examples/test_ssr_fft.py).
RTL_FIRST_FRAME = {(16, True): 42, (64, True): 111, (256, True): 352, (1024, True): 1278,
                   (16, False): 40, (64, False): 109, (256, False): 350, (1024, False): 1276}


@pytest.mark.parametrize("length,lanes", sorted(RTL_FIRST_FRAME))
def test_composite_pysim_tracks_the_rtl(tmp_path, length, lanes):
    """The per-task latencies are analytic (``latency_cycles``) with one trim against XSI; the
    first frame lands within 5 cycles of the RTL and frames follow every L/R, as in the RTL."""
    tb = run_pysim(tmp_path, length=length, n_frames=3, reorder="pingpong", lanes=lanes)
    cyc = pysim_frame_cycles(tb)
    assert abs(cyc[0] - RTL_FIRST_FRAME[(length, lanes)]) <= 5, cyc
    assert np.allclose(np.diff(cyc), length // 4), cyc


def _observed(tb):
    """Everything a flat pysim must reproduce: each sink's frames (when, in cycles, and what)."""
    p = tb.clk.period
    return [([round(float(t) / p, 6) for t in s.stamps], [np.asarray(w).tolist() for w in s.words])
            for s in tb.sinks]


@pytest.mark.parametrize("gaps", ((), (0, 37, 0, 120, 5)))
@pytest.mark.parametrize("inverse", (False, True))
@pytest.mark.parametrize("lanes", (False, True))
@pytest.mark.parametrize("length", (16, 64))
def test_flat_pysim_is_the_task_chain(tmp_path, length, lanes, inverse, gaps):
    """``pysim="flat"`` -- one process for the FFT -- reproduces the per-task chain's frames: the
    same bits at the same cycles, back to back and with idle gaps between frames."""
    kw = dict(length=length, n_frames=6, reorder="pingpong", lanes=lanes, inverse=inverse,
              burst_gaps=list(gaps))
    tasks = run_pysim(tmp_path / "tasks", pysim="tasks", **kw)
    flat = run_pysim(tmp_path / "flat", pysim="flat", **kw)
    assert _observed(flat) == _observed(tasks)
    assert len(flat.dut.tasks) == len(tasks.dut.tasks)       # the children still exist, passive


def test_flat_pysim_refuses_the_sob_reorder():
    """The stream-of-blocks reader is slower than its input (L/R + 5 a frame), so frames queue
    inside the chain; the flat model would not reproduce that queue, and says so."""
    with pytest.raises(ValueError, match="ping-pong reorder only"):
        SsrFft(name="f", sim=Simulation(), L=16, reorder="sob", pysim="flat")
