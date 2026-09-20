"""Bit-exact Python models of AMD Vitis L1 library blocks.

Given the same input bits, these reproduce the same output bits as the vendor HLS library,
without running Vitis.  They exist so a datapath built on a Vitis block can be designed and
simulated at Python speed while still predicting exactly what the FPGA emits.

Two blocks are modelled:

* :mod:`~waveflow.vitis_l1.fft` -- the **DSP L1 SSR FFT**, fixed point.  ``fft16`` covers all
  three scaling modes at ``L=16``; ``fft_general`` covers any ``L = 4^S`` under ``NO_SCALING``.
  Supporting constants live in :mod:`~waveflow.vitis_l1.twiddle` (including the quarter-wave
  reconstruction the hardware actually uses past ``L=16``) and the two complex primitives the
  butterfly needs in :mod:`~waveflow.vitis_l1.cxquant`.
* :mod:`~waveflow.vitis_l1.gemv` -- the **BLAS L1 matrix-vector multiply**.  What makes a naive
  model wrong is not the operations but the *order*: ``dot_tree`` is a tree within a beat, a
  tree within a chunk of beats, and a sequential accumulation across chunks.  The fixed-point
  path is in :mod:`~waveflow.vitis_l1.fixed`.

Scope, stated because "bit-exact" invites over-reading:

=========  ==========================================================================
FFT        fixed point, ``R=4``, ``L=4^S``, NATURAL order, FORWARD, TRN butterfly
           rounding.  *Not* radix 2/8/16, the forked sizes (32/128/512), float, or
           inverse.
GEMV       L1 ``gemv``: ``dot_tree`` (float/double), ``dot_dsp`` (integer), and
           ``ap_fixed``.  *Not* L2 ``krnl_gemv``, which is a multi-channel DDR kernel.
=========  ==========================================================================

The L2 FFT needs no separate model: ``L2/.../fft_kernel.hpp`` wraps the *identical* L1 core in
an AXI-MM layer whose ``readLines``/``writeLines`` only bit-slice an ``ap_uint<512>``
super-sample, with no arithmetic.  The L1 model is therefore already bit-exact for L2's
datapath; what L2 adds is packing and framing.

Everything here is verified against goldens produced by the vendor's own code -- never a
reimplementation -- and compared as raw stored integers rather than floats, because a float
comparison absorbs exactly the 1-LSB differences these models exist to predict.  The gates,
golden generators and Vitis verification projects are in ``tests/vitis_l1/``.
"""
from __future__ import annotations

from . import cxquant, fft, fixed, gemv, twiddle
from .fft import GROW_TO_MAX_WIDTH, NO_SCALING, SCALE, fft16, fft_general
from .gemv import adder_delays, dot, gemv_ab, gemv_int

__all__ = [
    "cxquant", "fft", "fixed", "gemv", "twiddle",
    "fft16", "fft_general", "NO_SCALING", "SCALE", "GROW_TO_MAX_WIDTH",
    "dot", "gemv_ab", "gemv_int", "adder_delays",
]
