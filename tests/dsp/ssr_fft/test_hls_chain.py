"""F2 of ``plans/ssr_fft.md``: the task bodies, chained by hand, in Vitis csim and csynth.

The chain is every task up to the last stage -- the transposer's commutators, each stage, the
commutator after it -- fed six frames back to back.  Its output is the last stage's digit-reversed
order, compared word for word with :func:`waveflow.dsp.ssr_fft.model.pipeline`.  (The reorder's SOB
pair is judged at RTL, F3: csim of ``hls::task`` + ``stream_of_blocks`` is not authoritative -- see
the note in ``hls_chain.render_top``.)

Measured 2026-10-05, Vitis HLS 2025.1, xczu48dr at 4 ns: bit-exact at L = 16, 64, 256, 1024; every
task II = 1 at L = 64.  The chain uses the generated wrappers, as the real top does.
"""
from __future__ import annotations

import re
from pathlib import Path

import numpy as np
import pytest

from waveflow.dsp.ssr_fft.model import Geometry
from waveflow.toolchain import toolchain

from .hls_chain import expected, read_out, write

pytestmark = pytest.mark.vitis

N_FRAMES = 6


def _run(root: Path, length: int, *, csynth: bool) -> str:
    if not toolchain.find_vitis_path():
        pytest.skip("Vitis installation not found")
    write(Geometry(length), root, N_FRAMES, csynth=csynth, natural=False)
    r = toolchain.run_vitis_hls(root / "run.tcl", work_dir=root)
    return (r.stdout or "") + (r.stderr or "")


@pytest.mark.parametrize("length", (16, 64, 1024))
def test_chain_csim_is_bit_exact_frame_after_frame(tmp_path_factory, length):
    root = tmp_path_factory.mktemp(f"ssr{length}")
    log = _run(root, length, csynth=(length == 64))
    assert "GATE_CSIM_DONE" in log, log[-3000:]
    geo = Geometry(length)
    want_re, want_im = expected(geo, N_FRAMES, natural=False)
    got_re, got_im = read_out(geo, root, N_FRAMES)
    n = geo.n_words
    for f in range(N_FRAMES):
        sl = slice(f * n, (f + 1) * n)
        assert np.array_equal(got_re[sl], want_re[sl]) and np.array_equal(got_im[sl], want_im[sl]), \
            f"frame {f}"
    if length == 64:
        assert "GATE_CSYNTH_DONE" in log, log[-3000:]
        rpt = (root / "proj" / "sol" / "syn" / "report" / "csynth.rpt").read_text(encoding="utf-8")
        # Timing is gated on the REAL top (test_xsi.py): here the first commutator reads a top-level
        # AXIS port directly, a path the real top does not have (lanes_in sits in front), and it
        # alone misses 250 MHz by 0.16 ns.
        loops = re.findall(r"^\s*\|\s*o \S+\s*\|.*?\|\s*(\d+)\|\s*(\d+)\|\s*\S*\|\s*(yes|no)\|", rpt,
                           flags=re.M)
        assert loops and all(ii == "1" and p == "yes" for _, ii, p in loops), loops
