"""A full-rate SSR (super-sample-rate) FFT: ``R`` samples per cycle, a new frame every ``L/R``.

The arithmetic is AMD's Vitis L1 SSR FFT's, bit for bit (``waveflow.vitis_l1.fft`` is the
reference); the data movement is rebuilt as free-running tasks so frames overlap.  See
``plans/ssr_fft.md``.

* :mod:`.model` -- the transform as the hardware computes it: per-stage butterflies on the
  stream's wire order, and the commutators and reorder as pure re-indexing.
* :mod:`.types` -- ``RadixWord``, the stream unit, and the per-edge formats.
* :mod:`.cycle_ref` -- a cycle-by-cycle reference of the commutator the HLS task implements.
"""
from .model import Geometry, ssr_fft

__all__ = ["Geometry", "ssr_fft"]
