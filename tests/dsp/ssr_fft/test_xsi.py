"""F3 of ``plans/ssr_fft.md``, the RTL rung: ``SsrFft`` through csynth and XSI.

The ``ap_ctrl_none`` top that ``composite_top_spec`` derives from the module (12 ``hls::task``s at
L = 64), driven by the BFM harness ``tb_top_spec`` derives from the testbench graph, timed from the
BFMs' capture bundles, for the RFSoC 4x2 at 250 MHz.  Both reorders:

* every frame bit-exact with ``VitisFft``'s golden, and every frame out (the burst drains);
* the frame interval: **L/R exactly** with the ping-pong reorder; L/R + 5 with the SOB pair, whose
  reader re-fires once a frame;
* timing met at 250 MHz on the real top (worst slack >= 0);
* the inverse (ping-pong): the same three -- its golden is the library's inverse arithmetic with
  the exact ``1/L``, and its interval is the forward's, since it is the same pipeline with other ROMs.

Measured 2026-10-05: ping-pong done = 113, 129, ... (interval 16); SOB 115, 135, ... (20).  For
scale: ``VitisFft``, AMD's core as shipped, runs 120 cycles a frame at L = 64.

Builds into a short directory under the repo (csynth fails silently past the Windows path limit) and
re-csynths only when the RTL is missing or stale.  Run: ``pytest tests/dsp/ssr_fft/test_xsi.py -m xsi``.
"""
from __future__ import annotations

import re
from pathlib import Path

import numpy as np
import pytest

from waveflow.build.trace_steps import rtl_staleness
from waveflow.dsp.ssr_fft import rtl
from waveflow.toolchain.toolchain import find_vitis_path, find_vivado_path

pytestmark = pytest.mark.xsi

ROOT = Path(__file__).resolve().parents[3] / "work" / "ssr_fft"
L, N_FRAMES = 64, 8
EXPECTED_INTERVAL = {"pingpong": L // 4, "sob": L // 4 + 5}


@pytest.fixture(scope="module", params=[("pingpong", False), ("sob", False), ("pingpong", True)],
                ids=["pingpong", "sob", "pingpong-inverse"])
def run(request):
    if not (find_vivado_path() and find_vitis_path()):
        pytest.skip("XSI gate prerequisite missing: Vitis HLS + Vivado")
    reorder, inverse = request.param
    root = ROOT / f"L{L}_{reorder}{'_inv' if inverse else ''}"
    root.mkdir(parents=True, exist_ok=True)
    rtl.generate(root, L, n_frames=N_FRAMES, reorder=reorder, inverse=inverse)
    if not (root / f"{rtl.TOP}_proj").is_dir() or rtl_staleness(root, rtl.TOP) is not None:
        rtl.synth(root)
    rtl.run_xsi(root)
    return reorder, inverse, root


def test_every_frame_out_and_bit_exact(run):
    _, inverse, root = run
    assert rtl.check_bits(root, L, N_FRAMES, inverse=inverse) == [True] * N_FRAMES


def test_frame_interval(run):
    reorder, _, root = run
    done = [f["done"] for f in rtl.frame_times(root, L)]
    assert len(done) == N_FRAMES
    assert set(np.diff(done)) == {EXPECTED_INTERVAL[reorder]}, done


def test_timing_met_at_250_mhz(run):
    _, _, root = run
    rpt = (root / f"{rtl.TOP}_proj" / "solution1" / "syn" / "report" / "csynth.rpt").read_text(
        encoding="utf-8")
    slacks = [float(s) for s in re.findall(r"^\s*\|\s*\+ \S+\s*\|\s*\S+\|\s*(-?\d+\.\d+)\|", rpt,
                                           flags=re.M)]
    assert slacks and min(slacks) >= 0, slacks
