"""ssr_fft.py -- Waveflow's full-rate SSR FFT, frames in and frames out.

``SsrFft`` (:mod:`waveflow.dsp.ssr_fft.hw`) is the module: ``VitisFft``'s port group (four lanes in,
four out), the vendor library's arithmetic bit for bit, and a new frame every ``L/R`` cycles -- one
free-running ``hls::task`` per piece of hardware.  The testbench graph around it is framework too
(:mod:`waveflow.dsp.ssr_fft.testbench`); this example is a worked use of both::

    StreamDriver x R  ->  SsrFft  ->  StreamSink x R

One graph, two backends: the pysim here, and the XSI harness ``ssr_fft_build.py`` generates from the
same object.  Frames back to back, so the interval is visible -- see ``docs/examples/ssr_fft/``.

    python -m examples.ssr_fft.ssr_fft          # pysim: bits + timing, no toolchain
"""
from __future__ import annotations

from pathlib import Path

from waveflow.dsp.ssr_fft.testbench import (  # noqa: F401  (the example's vocabulary)
    R,
    SsrFftTB,
    golden,
    pysim_frame_cycles,
    pysim_output,
    write_scenario,
)
from waveflow.dsp.ssr_fft.testbench import run_pysim as _run_pysim

HERE = Path(__file__).resolve().parent

#: The gated configuration: L=64 is 11 tasks and a 75-second csynth.
L = 64
N_FRAMES = 8
#: The reorder that reaches L/R; "sob" is the module's default and runs at L/R + 4.
REORDER = "pingpong"


def run_pysim(root: Path = HERE, **kw) -> SsrFftTB:
    """Run the testbench at the example's configuration, writing its scenario under *root*."""
    return _run_pysim(root, **{"length": L, "n_frames": N_FRAMES, "reorder": REORDER, **kw})


if __name__ == "__main__":
    import numpy as np

    tb = run_pysim()
    got, want = pysim_output(tb), golden(N_FRAMES, L)
    ok = all(np.array_equal(g[0], w[0]) and np.array_equal(g[1], w[1]) for g, w in zip(got, want))
    cyc = [round(c) for c in pysim_frame_cycles(tb)]
    print(f"{len(got)} frames, bit-exact vs golden: {ok}")
    print("frames done at cycles:", cyc)
    print("interval:", sorted(set(np.diff(cyc).tolist())), f"(L/R = {L // R})")
