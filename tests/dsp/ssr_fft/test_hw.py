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
#: (Frames with gaps between them: RTL_GAPPED below.)
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


#: XSI, RFSoC 4x2 at 250 MHz, ping-pong reorder, runtime direction (frames 0 1 1 0 1 ...), Vitis / xsim
#: 2025.1 (2026-11-22, ``plans/osfb_pysim_fidelity.md`` Section 6; ``python -m
#: examples.os_filterbank.fidelity --fft-gaps 64 16 256 1024``): the interval between consecutive output
#: frames when the input frames are a constant gap apart -- ``{gap: interval}``, the same for every frame
#: -- and for 24 frames at random gaps (a third 0, the rest 1 .. 2L/R): the gaps and each interval.
#: At L >= 64 every interval is ``max(2L/R, L/R + g)`` (``L/R`` at ``g = 0``), the transposer's last
#: commutator's rhythm; at L = 16 the intervals move with arrival phase (lists: frame by frame).
RTL_GAPPED = {
    16: {"const": {0: 4, 1: [8, 4] * 5 + [8], 2: [8, 4] * 5 + [8], 3: [8, 8, 8, 4] * 2 + [8, 8, 8], 4: 8,
                   6: 10, 8: 12},
         "gaps": [0, 1, 3, 3, 4, 3, 6, 5, 1, 5, 8, 1, 6, 1, 0, 0, 0, 7, 2, 8, 0, 0, 0],
         "rtl": [4, 8, 4, 8, 8, 8, 8, 9, 8, 8, 10, 8, 8, 4, 4, 4, 4, 11, 8, 10, 4, 4, 4]},
    64: {"const": {0: 16, 1: 32, 2: 32, 4: 32, 8: 32, 15: 32, 16: 32, 24: 40, 32: 48},
         "gaps": [0, 27, 0, 0, 0, 0, 5, 13, 5, 16, 24, 19, 21, 0, 20, 0, 31, 2, 0, 0, 8, 0, 30],
         "rtl": [16, 43, 16, 16, 16, 16, 32, 32, 32, 32, 40, 35, 37, 16, 36, 16, 47, 32, 16, 16, 32, 16, 46]},
    256: {"const": {0: 64, 1: 128, 2: 128, 4: 128, 32: 128, 63: 128, 64: 128, 96: 160, 128: 192},
          "gaps": [0, 0, 0, 0, 15, 0, 7, 65, 0, 0, 114, 11, 0, 0, 0, 55, 93, 70, 0, 5, 0, 29, 0],
          "rtl": [64, 64, 64, 64, 128, 64, 128, 129, 64, 64, 178, 128, 64, 64, 64, 128, 157, 134, 64, 128, 64,
                  128, 64]},
    1024: {"const": {0: 256, 1: 512, 2: 512, 4: 512, 128: 512, 255: 512, 256: 512, 384: 640, 512: 768},
           "gaps": [48, 0, 110, 41, 0, 0, 73, 0, 410, 0, 316, 0, 34, 500, 177, 0, 167, 0, 425, 0, 110, 0, 0],
           "rtl": [512, 256, 512, 512, 256, 256, 512, 256, 666, 256, 572, 256, 512, 756, 512, 256, 512, 256, 681,
                   256, 512, 256, 256]},
    # Measured at every gap below, the same way; pinned at three of them over 6 frames (a run costs
    # seconds at this length).
    4096: {"const": {0: 1024, 1: 2048, 1536: 2560}, "frames": 6,
           "gaps": [154, 0, 0, 1956, 0, 739, 0, 1279, 897, 677, 454, 629, 1253, 1981, 1685, 202, 0, 0, 1868, 0,
                    1474, 693, 267],
           "rtl": [2048, 1024, 1024, 2980, 1024, 2048, 1024, 2303, 2048, 2048, 2048, 2048, 2277, 3005, 2709, 2048,
                   1024, 1024, 2892, 1024, 2498, 2048, 2048]},
}


def _gapped_intervals(tmp_path, length: int, gaps: list[int]) -> list[int]:
    tb = run_pysim(tmp_path, length=length, n_frames=len(gaps) + 1, burst_gaps=list(gaps), reorder="pingpong",
                   direction="runtime", dirs=[0, 1, 1, 0, 1])
    return [round(float(x)) for x in np.diff(pysim_frame_cycles(tb))]


@pytest.mark.parametrize("length", (64, 256, 1024, 4096))
def test_composite_pysim_keeps_the_rtl_interval_after_a_gap(tmp_path, length):
    """A frame that follows its predecessor after any idle cycle leaves ``max(2L/R, L/R + g)`` after it,
    as the RTL's transposer's last commutator lets it: every interval at RTL, constant gaps and random
    ones, exactly (``hw.GapRhythm``).  Back to back stays ``L/R``."""
    rec = RTL_GAPPED[length]
    n = rec.get("frames", 12) - 1
    for g, want in rec["const"].items():
        assert _gapped_intervals(tmp_path / f"g{g}", length, [g] * n) == [want] * n, g
    assert _gapped_intervals(tmp_path / "random", length, rec["gaps"]) == rec["rtl"]


def test_composite_pysim_at_l16_is_within_three_cycles_a_frame(tmp_path):
    """At L = 16 the RTL's intervals after a gap move with arrival phase (a frame's latency depends on the
    next frame's gap too), and the model keeps ``L/R + g``: within 3 cycles of the RTL's interval, frame
    by frame; on the random run the total is the RTL's; at constant gaps of 1 to 3 cycles the RTL falls
    behind by up to 13 cycles over 11 frames (a 1-cycle gap)."""
    rec = RTL_GAPPED[16]
    for g, want in rec["const"].items():
        got = _gapped_intervals(tmp_path / f"g{g}", 16, [g] * 11)
        want = want if isinstance(want, list) else [want] * 11
        assert got == [4 + g] * 11, g                       # the model: L/R + g
        assert max(abs(a - b) for a, b in zip(got, want)) <= 3, (g, got, want)
        assert 0 <= sum(want) - sum(got) <= 13, (g, got, want)
    got = _gapped_intervals(tmp_path / "random", 16, rec["gaps"])
    assert max(abs(a - b) for a, b in zip(got, rec["rtl"])) <= 3
    assert sum(got) == sum(rec["rtl"])


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
