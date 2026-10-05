"""S3 of ``plans/vitis_l1_hwmodule.md``: ``VitisFft`` at RTL, under XSI.

The free-running ``ap_ctrl_none`` top that ``composite_top_spec`` derives from the module itself --
four AXI-Stream lanes in, four out, the vendor's ``xf::dsp::fft::fft<>`` inside an ``hls::task`` --
driven by the BFM harness that ``tb_top_spec`` derives from the testbench graph in
``examples/vitis_fft/vitis_fft.py``.  Four frames back to back.

Gates: every frame bit-exact against the golden; the RTL's per-frame completion cycles, recorded
exactly; and the pysim's frame intervals equal to the RTL's, so a wrong II cannot hide behind a
constant offset.

Run: ``pytest tests/examples/test_vitis_fft_xsi.py -m xsi`` (needs Vivado, and
``python -m examples.vitis_fft.vitis_fft_build`` for the csynth'd top).
"""
from __future__ import annotations

import subprocess

import numpy as np
import pytest

from examples.vitis_fft.vitis_fft import (
    L,
    N_FRAMES,
    R,
    frames_from_lanes,
    golden,
    pysim_frame_cycles,
    run_pysim,
)
from examples.vitis_fft.vitis_fft_build import HERE as ROOT
from examples.vitis_fft.vitis_fft_build import TB, TOP, XSI_DIR, generate
from waveflow.build.trace_steps import rtl_staleness, xsi_runner_cmd
from waveflow.toolchain.toolchain import find_vivado_path
from waveflow.utils.burst_io import read_burst_bundle

XSI = ROOT / XSI_DIR

#: The cycle (the sink's own 1-based count) at which each frame's last output word arrived, all
#: lanes.  Recorded 2026-10-05; a change is a real behaviour change -- look at it.  Frames leave every
#: 42 cycles: the free-running top does NOT overlap frames in the core, any more than the cosim'd
#: ap_ctrl_hs top did (interval 46 there; the 4 cycles were that top's per-call adapter).
EXPECTED_FRAME_CYCLES = [45, 87, 129, 171]
#: The RTL's first input beat lands in the sink's cycle 2, where the pysim's lands at t = 0, so the
#: two agree up to this constant.  Read off the waveform: TVALID && TREADY on s_in_*, see
#: ``docs/examples/vitis_fft/index.md``.
RTL_START_OFFSET = 1


def _lane_capture(j: int) -> tuple[np.ndarray, np.ndarray]:
    d = XSI / "vectors" / f"m_out_{j}"
    words = np.concatenate(read_burst_bundle(d)) if (d / "words.bin").exists() else np.zeros(0)
    cycles = np.fromfile(d / "cycles.bin", dtype="<u8") if (d / "cycles.bin").exists() \
        else np.zeros(0)
    return words.astype(np.uint64), cycles.astype(np.int64)


@pytest.fixture(scope="module")
def fft_run():
    if not find_vivado_path():
        pytest.skip("XSI gate prerequisite missing: Vivado (xvlog/xelab/xsim)")
    # include/, gen/ and xsi/ are untracked build output: regenerate (Python only) so the staleness
    # check compares the RTL against THIS checkout's sources (plans/source_layout.md).
    generate(ROOT)
    if not (ROOT / f"{TOP}_proj").is_dir() or not (XSI / f"rtl_{TOP}.f").exists():
        pytest.skip("XSI gate prerequisite missing: no csynth RTL -- run "
                    "python -m examples.vitis_fft.vitis_fft_build")
    stale = rtl_staleness(ROOT, TOP)
    if stale is not None:
        pytest.skip(f"XSI gate prerequisite missing: {stale}")
    for j in range(R):                     # a stale capture would let a broken run "pass"
        d = XSI / "vectors" / f"m_out_{j}"
        if d.exists():
            for f in d.iterdir():
                f.unlink()
    proc = subprocess.run(xsi_runner_cmd(TOP, TB), cwd=XSI, capture_output=True, text=True,
                          timeout=3600)
    assert proc.returncode == 0, f"XSI run failed\n{proc.stdout[-3000:]}\n{proc.stderr[-2000:]}"
    caps = [_lane_capture(j) for j in range(R)]
    per_lane = L // R
    assert all(w.size == per_lane * N_FRAMES for w, _ in caps), (
        f"lanes captured {[w.size for w, _ in caps]} words, expected {per_lane * N_FRAMES} each -- "
        f"the run did not complete\n{proc.stdout[-2000:]}")
    return caps


def _rtl_frame_cycles(caps) -> list[int]:
    per_lane = L // R
    return [int(max(c[(k + 1) * per_lane - 1] for _, c in caps)) for k in range(N_FRAMES)]


@pytest.mark.xsi
def test_vitis_fft_rtl_bit_exact(fft_run):
    from waveflow.vitis_l1.hw import VitisFft
    from waveflow.simulation.simulation import Simulation
    out_w = int(VitisFft(name="w", sim=Simulation(), L=L).out_fmt.W)
    got = frames_from_lanes([w for w, _ in fft_run], out_w)
    mask = (1 << out_w) - 1
    for k, ((g_re, g_im), (w_re, w_im)) in enumerate(zip(got, golden())):
        assert np.array_equal(g_re & mask, w_re & mask) and np.array_equal(g_im & mask, w_im & mask), \
            f"frame {k}: RTL output differs from the golden"


@pytest.mark.xsi
def test_vitis_fft_rtl_cycles(fft_run):
    cycles = _rtl_frame_cycles(fft_run)
    assert cycles == EXPECTED_FRAME_CYCLES


@pytest.mark.xsi
def test_vitis_fft_pysim_matches_rtl(fft_run, tmp_path):
    """The pysim, configured with the example's measured latency and II, puts every frame where the
    RTL does, up to one constant: the cycle the harness's first input beat lands in.  So the
    intervals are exact (a wrong II cannot hide) and so is the first frame's latency."""
    rtl = _rtl_frame_cycles(fft_run)
    py = [round(c) for c in pysim_frame_cycles(run_pysim(tmp_path))]
    assert [r - p for r, p in zip(rtl, py)] == [RTL_START_OFFSET] * N_FRAMES,         f"pysim {py} vs RTL {rtl}"
