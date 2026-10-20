"""lanes.py — lane groups and the packing of complex matrices into message words.

Two layouts, each with a C++ twin:

* **Lane groups** (``wf_lanes.h``): inside a component, a matrix is held as groups of ``L``
  complex values, row-major, so ``L`` columns are touched per cycle.  Lane ``l`` of a group sits
  at bits ``[2W·l, 2W·l + 2W)``: the real part in the low ``W`` bits and the imaginary part in the
  high ``W`` bits, the :class:`~waveflow.hw.complexfield.ComplexField` order.  The conversion is a
  reinterpretation of stored bits, never arithmetic.
* **Message words** (``wf_matrix_io.h``): between components, a matrix travels row-major as
  memory elements (:func:`~waveflow.linalg.formats.mem_format`), as many to a word as fit.

The values handled here are the **stored integers** of the register format.
"""

from __future__ import annotations

import sys

import numpy as np

from waveflow.hw.complexfield import ComplexField
from waveflow.hw.dataschema import DataArray, IntField
from waveflow.linalg.formats import DEFAULT_LANE_BITS, mem_elem_type, mem_format
from waveflow.utils import complexutils as cx
from waveflow.utils.fixputils import Format

#: The message word widths the generated array utilities support.
WORD_BITS_SUPPORTED = (32, 64, 128)
DEFAULT_WORD_BITS = 64


# --- lane groups -------------------------------------------------------------------------------


def group_type(W: int, L: int) -> type[IntField]:
    """One lane group: ``L`` complex ``W``-bit values in ``2·W·L`` bits."""
    return IntField.specialize(bitwidth=2 * int(W) * int(L), signed=False)


def block_type(W: int, n_groups: int, L: int) -> type[DataArray]:
    """A matrix as ``n_groups`` lane groups of ``L`` complex ``W``-bit values."""
    return DataArray.specialize(
        element_type=group_type(W, L), max_shape=(int(n_groups),), member_name="groups"
    )


def pack_group(re, im, W: int) -> int:
    """The bits of one lane group from ``L`` stored integers each (twin of ``wf_lanes::pack``)."""
    mask = (1 << int(W)) - 1
    g = 0
    for lane, (r, i) in enumerate(zip(re, im, strict=True)):
        g |= ((int(r) & mask) | ((int(i) & mask) << W)) << (2 * W * lane)
    return g


def unpack_group(g: int, W: int, L: int) -> tuple[list[int], list[int]]:
    """The signed stored integers of one lane group (twin of ``wf_lanes::unpack``)."""
    mask, sign = (1 << int(W)) - 1, 1 << (int(W) - 1)

    def signed(v: int) -> int:
        return v - (1 << W) if v & sign else v

    re = [signed((g >> (2 * W * lane)) & mask) for lane in range(int(L))]
    im = [signed((g >> (2 * W * lane + W)) & mask) for lane in range(int(L))]
    return re, im


def pack_groups(re, im, W: int) -> np.ndarray:
    """:func:`pack_group` over many groups at once: *re*, *im* of shape ``(n, L)`` -> the groups as
    stream words, ``(n,)`` ``uint64`` up to 64 bits, else ``(n, k)`` chunks, chunk 0 low.  Each lane's
    ``2W``-bit field is placed with shifts, whole arrays at a time (no Python loop a group)."""
    re = np.asarray(re, np.int64)
    im = np.asarray(im, np.int64)
    n, L = re.shape
    W = int(W)
    mask = np.uint64((1 << W) - 1)
    field = (re.astype(np.uint64) & mask) | ((im.astype(np.uint64) & mask) << np.uint64(W))
    bits = 2 * W * L
    k = -(-bits // 64)
    out = np.zeros((n, k), np.uint64)
    for lane in range(L):                       # a loop over lanes, not over groups
        off = 2 * W * lane
        c, sh = off // 64, off % 64
        out[:, c] |= field[:, lane] << np.uint64(sh)
        if sh + 2 * W > 64:                     # the field straddles two chunks
            out[:, c + 1] |= field[:, lane] >> np.uint64(64 - sh)
    return out[:, 0] if k == 1 else out


def unpack_groups(words, W: int, L: int) -> tuple[np.ndarray, np.ndarray]:
    """:func:`unpack_group` over many groups: stream words (``(n,)`` or ``(n, k)``) -> the signed stored
    integers ``(re, im)``, each ``(n, L)``."""
    w = np.asarray(words, np.uint64)
    if w.ndim == 1:
        w = w[:, None]
    W, L = int(W), int(L)
    fmask = np.uint64((1 << (2 * W)) - 1) if 2 * W < 64 else np.uint64(0xFFFFFFFFFFFFFFFF)
    fields = np.zeros((w.shape[0], L), np.uint64)
    for lane in range(L):
        off = 2 * W * lane
        c, sh = off // 64, off % 64
        f = w[:, c] >> np.uint64(sh)
        if sh + 2 * W > 64:
            f = f | (w[:, c + 1] << np.uint64(64 - sh))
        fields[:, lane] = f & fmask
    mask = np.uint64((1 << W) - 1)
    re = (fields & mask).astype(np.int64)
    im = ((fields >> np.uint64(W)) & mask).astype(np.int64)
    sign = 1 << (W - 1)
    re = np.where(re & sign, re - (1 << W), re)
    im = np.where(im & sign, im - (1 << W), im)
    return re, im


def n_groups(n_elems: int, L: int) -> int:
    """Lane groups holding ``n_elems`` complex values."""
    return -(-int(n_elems) // int(L))


def pack_matrix(re, im, W: int, L: int, n_grp: int | None = None) -> list[int]:
    """A matrix (any shape, row-major) → its lane groups, the last one zero-padded, and then
    zero groups up to ``n_grp`` (a block's size)."""
    re = np.asarray(re, np.int64).reshape(-1)
    im = np.asarray(im, np.int64).reshape(-1)
    n = n_groups(re.size, L) if n_grp is None else int(n_grp)
    if n * L < re.size:
        raise ValueError(f"{re.size} values do not fit {n} groups of {L}")
    pad = n * L - re.size
    re, im = np.pad(re, (0, pad)), np.pad(im, (0, pad))
    return [
        pack_group(re[g * L : (g + 1) * L], im[g * L : (g + 1) * L], W)
        for g in range(n)
    ]


def unpack_matrix(
    groups, n_elems: int, W: int, L: int
) -> tuple[np.ndarray, np.ndarray]:
    """The stored integers of the first ``n_elems`` values in ``groups``, as flat arrays."""
    re, im = [], []
    for g in list(groups)[: n_groups(n_elems, L)]:
        r, i = unpack_group(int(g), W, L)
        re += r
        im += i
    return np.asarray(re[:n_elems], np.int64), np.asarray(im[:n_elems], np.int64)


# --- message words -----------------------------------------------------------------------------


def elems_per_word(
    lane_bits: int = DEFAULT_LANE_BITS, word_bits: int = DEFAULT_WORD_BITS
) -> int:
    """Memory elements per message word."""
    if int(word_bits) not in WORD_BITS_SUPPORTED:
        raise ValueError(f"word width {word_bits} not in {WORD_BITS_SUPPORTED}")
    elem = 2 * int(lane_bits)
    if word_bits % elem:
        raise ValueError(
            f"a {word_bits}-bit word does not hold whole {elem}-bit elements"
        )
    return word_bits // elem


def nwords(
    n_elems: int, lane_bits: int = DEFAULT_LANE_BITS, word_bits: int = DEFAULT_WORD_BITS
) -> int:
    """Message words holding ``n_elems`` complex elements."""
    return -(-int(n_elems) // elems_per_word(lane_bits, word_bits))


def mem_array_type(
    fmt: Format, n: int, lane_bits: int = DEFAULT_LANE_BITS
) -> type[DataArray]:
    """``DataArray[ComplexField[FixedField<mem_format(fmt)>]]`` of ``n`` elements."""
    elem: type[ComplexField] = mem_elem_type(fmt, lane_bits)
    return DataArray.specialize(elem, max_shape=(int(n),))


#: Lane widths whose memory elements are a pair of machine integers: the words are then the
#: ``(re, im)`` pairs' own bytes, and the conversion is a reinterpretation of the buffer (one pass,
#: no ``DataArray``).  Any other width takes the general ``ComplexField`` path.
_DIRECT_LANE_DTYPE = {8: (np.int8, np.uint8), 16: (np.int16, np.uint16), 32: (np.int32, np.uint32)}


def _direct_dtype(fmt: Format, lane_bits: int):
    """The integer dtype of one lane when the direct path gives the general path's bits, else None:
    a machine-width lane on a little-endian host.  The memory format wraps (``mem_format``), and so
    does the cast to the lane's dtype."""
    pair = _DIRECT_LANE_DTYPE.get(int(lane_bits))
    if pair is None or sys.byteorder != "little":
        return None
    return pair[0] if mem_format(fmt, lane_bits).signed else pair[1]


def to_words(
    re,
    im,
    fmt: Format,
    lane_bits: int = DEFAULT_LANE_BITS,
    word_bits: int = DEFAULT_WORD_BITS,
) -> np.ndarray:
    """Stored integers ``(re, im)`` of format ``fmt`` (any shape, row-major) → message words."""
    epw = elems_per_word(lane_bits, word_bits)
    shift = int(lane_bits) - fmt.W
    dt = _direct_dtype(fmt, lane_bits)
    if dt is not None:
        re, im = np.asarray(re).reshape(-1), np.asarray(im).reshape(-1)
        nw = -(-re.size // epw)
        pairs = np.zeros((nw * epw, 2), dt)    # the last word zero-padded
        pairs[: re.size, 0] = re               # wraps to the lane, as the format does
        pairs[: re.size, 1] = im
        if shift:                              # the same bits as shifting before the wrap
            pairs <<= shift
        flat = pairs.reshape(-1)
        if int(word_bits) == 32:               # one 32-bit word to a uint64
            return flat.view(np.uint32).astype(np.uint64)
        w = flat.view(np.uint64)
        return w.reshape(nw, int(word_bits) // 64) if int(word_bits) > 64 else w.reshape(-1)
    re = np.asarray(re, np.int64).reshape(-1) << shift
    im = np.asarray(im, np.int64).reshape(-1) << shift
    arr = mem_array_type(fmt, re.size, lane_bits)(
        cx.make_complex(re, im, mem_format(fmt, lane_bits))
    )
    return np.asarray(arr.serialize(word_bw=int(word_bits)), dtype=np.uint64)


def from_words(
    words,
    n: int,
    fmt: Format,
    lane_bits: int = DEFAULT_LANE_BITS,
    word_bits: int = DEFAULT_WORD_BITS,
) -> tuple[np.ndarray, np.ndarray]:
    """Message words → the stored integers ``(re, im)`` of the first ``n`` elements, format ``fmt``.

    Raises if a value has fraction bits the register format cannot hold: the words were not
    written from that format.
    """
    elems_per_word(lane_bits, word_bits)
    shift = int(lane_bits) - fmt.W
    finer = ValueError(f"words hold values finer than the {fmt.W}-bit register format")
    dt = _direct_dtype(fmt, lane_bits)
    if dt is not None:
        w = np.asarray(words, dtype=np.uint64)
        buf = w.astype(np.uint32) if int(word_bits) == 32 else np.ascontiguousarray(w)
        pairs = buf.reshape(-1).view(dt).reshape(-1, 2)[: int(n)]
        if pairs.shape[0] < int(n):
            raise ValueError(f"{w.shape[0]} words hold fewer than {n} elements")
        if shift:
            if np.any(pairs & dt((1 << shift) - 1)):
                raise finer
            pairs = pairs >> shift
        return pairs[:, 0].astype(np.int64), pairs[:, 1].astype(np.int64)
    arr = mem_array_type(fmt, n, lane_bits)().deserialize(
        np.asarray(words, dtype=np.uint64), word_bw=int(word_bits)
    )
    pairs = np.ascontiguousarray(np.asarray(arr)).view(np.int64).reshape(-1, 2)
    re, im = pairs[:, 0].copy(), pairs[:, 1].copy()
    if np.any(re & ((1 << shift) - 1)) or np.any(im & ((1 << shift) - 1)):
        raise finer
    return re >> shift, im >> shift
