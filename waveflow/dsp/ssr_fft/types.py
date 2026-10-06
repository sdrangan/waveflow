"""``RadixWord`` -- the unit every SSR FFT stream carries: ``R`` complex samples, one per lane.

A ``RadixWord`` is a ``DataArray`` of ``R`` ``ComplexField[FixedField(W, I)]`` elements.
``ComplexField`` serializes as interleaved I/Q -- the real part in the low ``W`` bits, the layout of
``std::complex<ap_fixed<W, I>>`` -- and a ``DataArray`` puts element 0 in the low bits, so lane ``r``
of a word occupies bits ``[2*W*r, 2*W*(r+1))``.  Unpacked, a word is exactly AMD's
``SuperSampleContainer<R, T>``.

**The channel is still ``ap_uint<W_word>``** (a ``StreamIF`` carries raw words; the type lives at
the endpoints), with ``W_word = RadixWord.bitwidth = 2*W*R`` -- one word per beat.  A plain
``DataArray`` reports ``bitwidth = None``, so :class:`EdgeType` carries it explicitly.

The format differs per edge, because the stages grow the width; :func:`edge_types` derives every
edge's type from the :class:`~.model.Geometry`, never by hand.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import cache

from waveflow.hw.complexfield import ComplexField
from waveflow.hw.dataschema import DataArray
from waveflow.utils.fixputils import Format
from waveflow.vitis_l1.cxquant import complex_from_format

from .model import Geometry


@dataclass(frozen=True)
class EdgeType:
    """The types of one edge: its sample format, the element and word types, the word width."""
    name: str
    fmt: Format
    R: int

    @property
    def elem(self) -> type[ComplexField]:
        """One lane: ``std::complex<ap_fixed<W, I>>``."""
        return complex_from_format(self.fmt)

    @property
    def word(self) -> type[DataArray]:
        """The ``RadixWord``: ``R`` lanes."""
        return radix_word(self.fmt, self.R)

    @property
    def bitwidth(self) -> int:
        """Bits per stream word, ``2 * W * R``: the ``StreamIF`` bitwidth of this edge."""
        return self.elem.bitwidth * self.R


@cache
def radix_word(fmt: Format, r: int = 4) -> type[DataArray]:
    """``DataArray[ComplexField(fmt)]`` of shape ``(R,)`` -- one stream word."""
    return DataArray.specialize(complex_from_format(fmt), max_shape=(r,))


def edge_types(geo: Geometry) -> list[EdgeType]:
    """Every edge of the pipeline, in order: ``in``, ``tp0..``, ``st0``, ``cm0``, ..., ``st{S-1}``."""
    return [EdgeType(name, fmt, geo.R) for name, fmt in geo.edges()]
