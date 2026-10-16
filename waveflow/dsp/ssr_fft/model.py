"""The SSR FFT as the hardware computes it: stages on the stream's wire order.

``waveflow.vitis_l1.fft.fft_general`` computes the same bits on a flat array indexed by output
position and never commits to how samples travel.  This module does commit: a frame is a
``(L/R, R)`` array -- word ``w``, lane ``r`` -- and every block of the pipeline is a function on
that array:

* the **input transposer**: ``S - 1`` commutators, re-indexing only;
* **stage** ``s``: one radix-``R`` butterfly per word (lane ``p`` = butterfly input ``p``), then the
  twiddle rotation per lane (lane ``q`` = butterfly output ``q``) -- the only arithmetic;
* the **inter-stage commutator** after every stage but the last: re-indexing only;
* the **digit-reversal reorder**: re-indexing only.

The arithmetic is ``fft_general``'s, call for call (``_dft4``, ``complex_multiply``, the same
formats and the same narrowing points), so the bits cannot drift from the vendor reference.  The
re-indexing is where this module adds something: :func:`block_transpose_index` is the commutator
the HLS task implements (checked against a cycle-by-cycle model in :mod:`.cycle_ref`).

Every commutator is the same operation, an ``R x R`` **block transpose**: cut the stream into
groups of ``R`` slots of ``D`` words; slot ``i`` lane ``j`` moves to slot ``j`` lane ``i``.  A group
spans ``R*D <= L/R`` words, so a commutator never mixes frames.

**Inverse.**  ``Geometry(inverse=True)`` is the vendor's ``REVERSE_TRANSFORM`` arithmetic: the
same pipeline with conjugated twiddles and radix-4 constants (both ROMs, so no hardware changes).
Its ``1/L`` is where this design departs from the library, deliberately: the library divides by
``L`` at the old resolution (truncating toward zero, so the low ``log2 L`` bits are always 0); here
the bits are the unscaled transform's and only the binary point moves (``out_fmt`` has
``log2 L`` fewer integer bits than :attr:`Geometry.raw_out_fmt`) -- exact and free.
``waveflow.vitis_l1.fft.ifft_scale`` maps these bits onto the library's.

Scope: ``R = 4``, ``L = 4^S`` with ``S >= 2``, ``SSR_FFT_NO_SCALING`` -- what ``fft_general`` has
measured, in both directions.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import cache, cached_property

import numpy as np

from waveflow.utils import fixputils as fp
from waveflow.utils.fixputils import Format
from waveflow.vitis_l1.cxquant import complex_multiply
from waveflow.vitis_l1.fft import NO_SCALING, _dft4, _f, _log, exp_table_format, stage_formats
from waveflow.vitis_l1.fft import twiddle_stored

R = 4


@dataclass(frozen=True)
class Geometry:
    """Everything about an SSR FFT that follows from ``L`` and the formats.

    ``in_w, in_i`` is the input ``ap_fixed`` format; ``tw_w, tw_i`` the twiddle table's;
    ``inverse`` selects the inverse transform (``1/L`` included, see the module docstring).
    """
    L: int
    in_w: int = 16
    in_i: int = 2
    tw_w: int = 18
    tw_i: int = 2
    mode: str = NO_SCALING
    R: int = R
    inverse: bool = False

    def __post_init__(self) -> None:
        if self.R != R:
            raise NotImplementedError(f"R={self.R}: only R={R} is modelled.")
        if self.mode != NO_SCALING:
            raise NotImplementedError(f"{self.mode}: only {NO_SCALING} is modelled (fft_general).")
        if self.S < 2:
            raise ValueError(f"L={self.L}: need L = {R}^S with S >= 2.")

    # -- shape ----------------------------------------------------------------------------
    @cached_property
    def S(self) -> int:
        """Number of butterfly stages, ``log_R(L)``."""
        return _log(self.L, self.R)

    @property
    def n_words(self) -> int:
        """Words per frame, ``L/R``."""
        return self.L // self.R

    def m_count(self, s: int) -> int:
        """Butterflies per sub-transform at stage ``s``: ``L / R^(s+1)``."""
        return self.L // self.R ** (s + 1)

    @property
    def transposer_ds(self) -> list[int]:
        """Block sizes of the input transposer's commutators, in order: ``1, R, ..., R^(S-2)``."""
        return [self.R ** k for k in range(self.S - 1)]

    def stage_d(self, s: int) -> int:
        """Block size of the commutator after stage ``s`` (``s < S-1``): ``L / R^(s+2)``."""
        if not 0 <= s < self.S - 1:
            raise ValueError(f"stage {s} has no commutator after it (S={self.S}).")
        return self.L // self.R ** (s + 2)

    # -- formats --------------------------------------------------------------------------
    @cached_property
    def stage_fmts(self) -> list[tuple[Format, Format]]:
        """``(input, butterfly output)`` per stage -- ``fft_general``'s ``stage_formats``."""
        return stage_formats(self.in_w, self.in_i, self.S, self.mode)

    @property
    def in_fmt(self) -> Format:
        return _f(self.in_w, self.in_i)

    def stage_out_fmt(self, s: int) -> Format:
        """The format stage ``s`` writes: the next stage's input, or the output for the last."""
        if s < self.S - 1:
            return self.stage_fmts[s + 1][0]
        return self.raw_out_fmt

    @property
    def raw_out_fmt(self) -> Format:
        """What the last stage writes: the library's unscaled output format, either direction."""
        g_last = self.stage_fmts[-1][1]
        return _f(g_last.W - 1, g_last.int_bits)

    @property
    def out_fmt(self) -> Format:
        """The output format: :attr:`raw_out_fmt`, with the inverse's ``1/L`` as ``log2 L`` fewer
        integer bits -- the same stored bits, so the scaling costs nothing."""
        f = self.raw_out_fmt
        return _f(f.W, f.int_bits - _log(self.L, 2)) if self.inverse else f

    def edges(self) -> list[tuple[str, Format]]:
        """Every stream edge, in pipeline order, with the format it carries.

        ``in``, the transposer's ``tp*``, each stage's ``st*`` and commutator's ``cm*``, then the
        reorder's commutator ``rc`` and the natural-order ``out`` (the SOB between ``rc`` and
        ``out`` is a block channel, not a stream edge).  The last stage's edge carries
        :attr:`raw_out_fmt`; ``rc`` onwards carries :attr:`out_fmt` -- for the inverse, ``rc``
        reads the stage's words through the scaled type, which is where ``1/L`` happens.
        """
        out = [("in", self.in_fmt)]
        out += [(f"tp{k}", self.in_fmt) for k in range(self.S - 1)]
        for s in range(self.S):
            out.append((f"st{s}", self.stage_out_fmt(s)))
            if s < self.S - 1:
                out.append((f"cm{s}", self.stage_out_fmt(s)))
        out += [("rc", self.out_fmt), ("out", self.out_fmt)]
        return out

    # -- tables ---------------------------------------------------------------------------
    @cached_property
    def twiddles(self) -> tuple[np.ndarray, np.ndarray]:
        """``W_L^i`` as the hardware stores it, ``i = 0 .. L-1`` (quarter-wave reconstruction)."""
        return twiddle_stored(self.L, self.tw_w, self.tw_i, self.inverse)

    def twiddle_index(self, s: int) -> np.ndarray:
        """``[word, lane q]`` -> index into :attr:`twiddles` for stage ``s``'s rotation.

        Word ``w`` holds butterfly ``m = w mod m_count``; output lane ``q`` is rotated by
        ``W_L^(m q R^s)``, exactly ``fft_general``'s ``(m * q * tw_scale) % L``.
        """
        m = np.arange(self.n_words) % self.m_count(s)
        q = np.arange(self.R)
        return (m[:, None] * q[None, :] * self.R ** s) % self.L

    @cached_property
    def natural_index(self) -> np.ndarray:
        """``[word, lane]`` of the last stage's output -> its natural-order bin ``k``.

        The index recursion of ``fft_general`` alone: block ``(b, q)`` of the next level is
        ``blocks[b, q + R*u]``.  At the last level each block is one butterfly, word ``b``, and
        lane ``q`` of it is bin ``blocks[b, q]``.
        """
        blocks = np.arange(self.L).reshape(1, self.L)
        for _ in range(self.S - 1):
            n_blocks, sub = blocks.shape
            blocks = (blocks.reshape(n_blocks, sub // self.R, self.R)
                      .transpose(0, 2, 1).reshape(n_blocks * self.R, sub // self.R))
        return blocks.reshape(self.n_words, self.R)


# -------------------------------------------------------------------------------------------
# Re-indexing: commutators, transposer, reorder
# -------------------------------------------------------------------------------------------
@cache
def block_transpose_index(n_words: int, d: int, r: int = R) -> np.ndarray:
    """The commutator with block size ``d`` as a gather: ``out.flat = in.flat[idx]``.

    Output word ``g*R*d + j*d + e``, lane ``i`` takes input word ``g*R*d + i*d + e``, lane ``j``.
    """
    group = r * d
    if n_words % group:
        raise ValueError(f"{n_words} words is not a whole number of {r}x{d}-word groups.")
    w = np.arange(n_words)[:, None]
    i = np.arange(r)[None, :]
    g, j, e = w // group, (w % group) // d, w % d
    idx = (g * group + i * d + e) * r + j
    idx.setflags(write=False)
    return idx


def commute(frame: np.ndarray, d: int) -> np.ndarray:
    """Apply one commutator to a ``(L/R, R)`` frame (any dtype, structured included)."""
    n_words, r = frame.shape
    return frame.reshape(-1)[block_transpose_index(n_words, d, r)]


def to_words(x: np.ndarray, r: int = R) -> np.ndarray:
    """Natural-order samples -> the input wire order: sample ``n`` on word ``n // R``, lane ``n % R``."""
    return np.asarray(x).reshape(-1, r)


def transpose_in(geo: Geometry, frame: np.ndarray) -> np.ndarray:
    """The input transposer: word ``m`` lane ``p`` becomes ``x[m + p*L/R]``."""
    for d in geo.transposer_ds:
        frame = commute(frame, d)
    return frame


def reorder_d(geo: Geometry) -> int:
    """Block size of the reorder's commutator: ``L / R^2``, a transpose of the whole frame."""
    return geo.L // geo.R ** 2


def reorder_word_perm(geo: Geometry) -> np.ndarray:
    """Where the reorder writes each word (after its commutator): natural word ``perm[b]``.

    Keep the top base-``R`` digit of ``b`` and reverse the other ``S - 2``.  The identity at
    ``L = 16, 64``.  The C++ writer computes the same digits; the test checks both against
    :attr:`Geometry.natural_index`.
    """
    nd = geo.S - 1
    b = np.arange(geo.n_words)
    low = [(b // geo.R ** i) % geo.R for i in range(nd - 1)]           # LSD first
    top = b // geo.R ** (nd - 1)
    out = top * geo.R ** (nd - 1)
    for i, dgt in enumerate(reversed(low)):
        out = out + dgt * geo.R ** i
    return out


def reorder(geo: Geometry, frame: np.ndarray) -> np.ndarray:
    """The digit-reversal reorder, as the hardware does it: a whole-frame block transpose (a
    commutator, ``D = L/R^2``), then a whole-word permutation through a frame buffer (the SOB).

    The digit reversal moves the ``R`` lanes of one word onto the *same* lane of ``R`` different
    words, so it cannot be a word permutation alone; the transpose first puts every bin on its
    final lane.
    """
    t = commute(frame, reorder_d(geo))
    out = np.empty_like(t)
    out[reorder_word_perm(geo)] = t
    return out


# -------------------------------------------------------------------------------------------
# Arithmetic: one stage
# -------------------------------------------------------------------------------------------
def stage(geo: Geometry, s: int, re: np.ndarray, im: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Stage ``s`` on a ``(L/R, R)`` frame of stored integers: butterfly per word, then rotate.

    Every call below is ``fft_general``'s for the same stage, on the same values -- only the
    arrangement differs (words here, ``(q, block, m)`` there).
    """
    f_in, _ = geo.stage_fmts[s]
    # _dft4 takes axis 0 = butterfly input p; a word's lanes are exactly that.
    orr, oii, g = _dft4(re.T, im.T, f_in, s == 0, geo.mode, geo.tw_w, geo.tw_i,
                        geo.inverse)                                             # [q, word]
    if s < geo.S - 1:
        f_next = geo.stage_fmts[s + 1][0]
        op1 = g
        if (g.W, g.int_bits) != (f_next.W, f_next.int_bits):
            orr, oii = fp.quantize(orr, g, f_next), fp.quantize(oii, g, f_next)
            op1 = f_next
        tw_r, tw_i = geo.twiddles
        idx = geo.twiddle_index(s).T                                             # [q, word]
        orr, oii = complex_multiply(orr, oii, op1, np.asarray(tw_r)[idx], np.asarray(tw_i)[idx],
                                    exp_table_format(geo.tw_w, geo.tw_i), f_next)
    else:
        fout = geo.raw_out_fmt
        if (g.W, g.int_bits) != (fout.W, fout.int_bits):
            orr, oii = fp.quantize(orr, g, fout), fp.quantize(oii, g, fout)
    return (np.ascontiguousarray(np.asarray(orr, dtype=np.int64).T),
            np.ascontiguousarray(np.asarray(oii, dtype=np.int64).T))


# -------------------------------------------------------------------------------------------
# The whole pipeline
# -------------------------------------------------------------------------------------------
def pipeline(geo: Geometry, re: np.ndarray, im: np.ndarray, *, natural: bool = True
             ) -> tuple[np.ndarray, np.ndarray]:
    """One frame through every block, in wire order: ``(L/R, R)`` in, ``(L/R, R)`` out.

    ``natural=False`` stops before the reorder: the last stage's (digit-reversed) order.
    """
    re, im = transpose_in(geo, re), transpose_in(geo, im)
    for s in range(geo.S):
        re, im = stage(geo, s, re, im)
        if s < geo.S - 1:
            d = geo.stage_d(s)
            re, im = commute(re, d), commute(im, d)
    if natural:
        re, im = reorder(geo, re), reorder(geo, im)
    return re, im


def ssr_fft(x_re: np.ndarray, x_im: np.ndarray, length: int, in_w: int = 16, in_i: int = 2,
            tw_w: int = 18, tw_i: int = 2, inverse: bool = False
            ) -> tuple[np.ndarray, np.ndarray, Format]:
    """Natural-order samples in, natural-order bins out -- ``fft_general``'s signature.

    Computed through :func:`pipeline`, so equality with ``fft_general`` checks the wire order of
    every edge, not just the arithmetic.  For the inverse the bits equal
    ``fft_general(..., inverse=True, exact_scale=True)``.
    """
    geo = Geometry(length, in_w, in_i, tw_w, tw_i, inverse=inverse)
    re, im = pipeline(geo, to_words(np.asarray(x_re, dtype=np.int64)),
                      to_words(np.asarray(x_im, dtype=np.int64)))
    return re.reshape(-1), im.reshape(-1), geo.out_fmt
