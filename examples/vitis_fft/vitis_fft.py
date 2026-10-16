"""vitis_fft.py — the AMD Vitis L1 SSR FFT as a Waveflow module, frames in and frames out.

``VitisFft`` (:mod:`waveflow.vitis_l1.hw`) is the module: an ``R``-wide AXI-Stream port group each
side, bits from the bit-exact model in :mod:`waveflow.vitis_l1.fft`, a C++ body that calls the vendor's
``xf::dsp::fft::fft<>``, and timing from the calibrated RFSoC 4x2 platform.  The testbench graph around
it is framework too (:mod:`waveflow.vitis_l1.testbench`); this example is a worked use of both::

    StreamDriver x R  ->  VitisFft  ->  StreamSink x R

One graph, two backends: the pysim here, and the XSI harness ``vitis_fft_build.py`` generates from
the same object.  Several frames back to back, so the block's latency and its frame interval are
separately visible -- see ``docs/examples/vitis_fft/index.md``.

    python -m examples.vitis_fft.vitis_fft          # pysim: bits + timing, no toolchain
"""
from __future__ import annotations

from pathlib import Path

from waveflow.vitis_l1.testbench import (  # noqa: F401  (the example's vocabulary)
    CLK_HZ,
    R,
    VitisFftTB,
    default_platform_dir,
    frames_from_lanes,
    golden,
    pysim_frame_cycles,
    pysim_output,
    write_scenario,
)
from waveflow.vitis_l1.testbench import run_pysim as _run_pysim

HERE = Path(__file__).resolve().parent

#: The gated configuration.  L=16 keeps csynth and the XSI run short.
L = 16
N_FRAMES = 4


def run_pysim(root: Path = HERE, **kw) -> VitisFftTB:
    """Run the testbench at the example's configuration, writing its scenario under *root*."""
    return _run_pysim(root, **{"length": L, "n_frames": N_FRAMES, **kw})


if __name__ == "__main__":
    import numpy as np

    tb = run_pysim()
    got, want = pysim_output(tb), golden(N_FRAMES, L)
    ok = all(np.array_equal(g[0], w[0]) and np.array_equal(g[1], w[1]) for g, w in zip(got, want))
    print(f"{len(got)} frames, bit-exact vs golden: {ok}")
    print("frames done at cycles:", [round(c) for c in pysim_frame_cycles(tb)])
