"""ssr_fft_build.py -- generate and synthesize the example's ``SsrFft`` for the RFSoC 4x2.

    python -m examples.ssr_fft.ssr_fft_build             # generate, then csynth
    python -m examples.ssr_fft.ssr_fft_build --no-synth  # generate only (Python, seconds)

Everything is the framework's (:mod:`waveflow.dsp.ssr_fft.rtl`): the task bodies and the generated
configuration (:mod:`waveflow.dsp.ssr_fft.hls`), the ``ap_ctrl_none`` top from ``composite_top_spec``,
and the RTL run through the system flow (``rtl.run_xsi``).  No vendor headers, no include path.  This
example has no ``src/``: it has no hand-written C++ of its own; ``include/``, ``gen/`` and the run's
``system/`` are build output.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from waveflow.dsp.ssr_fft import rtl
from waveflow.dsp.ssr_fft.rtl import TOP  # noqa: F401  (re-exported)

from examples.ssr_fft.ssr_fft import L, N_FRAMES, REORDER

HERE = Path(__file__).resolve().parent


def generate(root: Path = HERE) -> str:
    return rtl.generate(root, L, n_frames=N_FRAMES, reorder=REORDER)


def synth(root: Path = HERE) -> str:
    return rtl.synth(root)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--no-synth", action="store_true")
    a = ap.parse_args()
    top = generate()
    print(f"generated gen/{top}.cpp and gen/{top}.tcl")
    if a.no_synth:
        return
    synth()
    print(f"csynth {top} OK; run it: python -m examples.ssr_fft.ssr_fft_measure --lengths {L}")


if __name__ == "__main__":
    main()
