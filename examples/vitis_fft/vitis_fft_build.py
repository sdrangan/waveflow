"""vitis_fft_build.py — generate and synthesize the example's ``VitisFft`` for the RFSoC 4x2.

    python -m examples.vitis_fft.vitis_fft_build             # generate, then csynth
    python -m examples.vitis_fft.vitis_fft_build --no-synth  # generate only (Python, seconds)

Everything is the framework's (:mod:`waveflow.vitis_l1.rtl`): the copied task body, the vendor include
path, the ``ap_ctrl_none`` top from ``composite_top_spec``, the XSI harness from the testbench graph.
This example has no ``src/`` because it has no hand-written C++ of its own, and ``include/``, ``gen/``
(with the ``.tcl``) and ``xsi/`` are build output (``plans/source_layout.md``).

Timing is **not** measured here: ``VitisFft`` is framework infrastructure, so its calibration lives on
the platform (``waveflow/calib/platforms/rfsoc4x2_bfm_250mhz``) and is produced by its fixture
(``waveflow/calib/fixtures/vitis_fft.py``).  The example's XSI gate checks the pysim against this RTL.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from waveflow.vitis_l1 import rtl
from waveflow.vitis_l1.rtl import TB, TOP, XSI_DIR  # noqa: F401  (re-exported for the gate)

from examples.vitis_fft.vitis_fft import L

HERE = Path(__file__).resolve().parent


def generate(root: Path = HERE) -> str:
    return rtl.generate(root, L)


def synth(root: Path = HERE) -> str:
    return rtl.synth(root)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--no-synth", action="store_true")
    a = ap.parse_args()
    top = generate()
    print(f"generated gen/{top}.cpp, gen/{top}.tcl and {XSI_DIR}/")
    if a.no_synth:
        return
    synth()
    print(f"csynth {top} OK")


if __name__ == "__main__":
    main()
