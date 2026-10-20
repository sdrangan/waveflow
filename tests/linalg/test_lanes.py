"""Step 7.1: lane groups and the packing of matrices into message words (Python side)."""

from __future__ import annotations

import numpy as np
import pytest

from waveflow.linalg import lanes as LN
from waveflow.utils.fixputils import Format, OMode, QMode


def reg(W: int, I: int) -> Format:
    return Format(W, I, True, QMode.AP_RND, OMode.AP_SAT)


def stored(W: int, n: int, seed: int) -> tuple[np.ndarray, np.ndarray]:
    """Random stored integers of a W-bit signed register, extremes included."""
    rng = np.random.default_rng(seed)
    lo, hi = -(1 << (W - 1)), (1 << (W - 1)) - 1
    re = rng.integers(lo, hi + 1, n)
    im = rng.integers(lo, hi + 1, n)
    k = min(n, 2)
    re[:k], im[:k] = (lo, hi)[:k], (hi, lo)[:k]
    return re, im


@pytest.mark.parametrize("W,L", [(8, 1), (8, 4), (12, 4), (16, 16)])
def test_pack_and_unpack_a_group(W, L):
    re, im = stored(W, L, seed=W * L)
    g = LN.pack_group(re, im, W)
    assert 0 <= g < 1 << (2 * W * L)
    assert LN.unpack_group(g, W, L) == ([int(v) for v in re], [int(v) for v in im])
    # lane 0's real part is the lowest W bits, its imaginary part the next W
    assert g & ((1 << W) - 1) == int(re[0]) & ((1 << W) - 1)
    assert (g >> W) & ((1 << W) - 1) == int(im[0]) & ((1 << W) - 1)


def test_block_type_holds_the_groups():
    blk = LN.block_type(12, 8, 4)
    assert blk.max_shape == (8,)
    assert LN.group_type(12, 4).bitwidth == 96


@pytest.mark.parametrize(
    "lane_bits,word_bits,W,I",
    [
        (8, 32, 8, 2),
        (8, 64, 8, 2),
        (16, 32, 8, 2),
        (16, 64, 8, 2),
        (16, 32, 12, 3),
        (16, 64, 12, 3),
        (16, 64, 16, 5),
    ],
)
def test_words_round_trip(lane_bits, word_bits, W, I):
    fmt = reg(W, I)
    re, im = stored(W, 37, seed=lane_bits + word_bits + W)
    words = LN.to_words(re, im, fmt, lane_bits, word_bits)
    assert len(words) == LN.nwords(37, lane_bits, word_bits)
    assert LN.elems_per_word(lane_bits, word_bits) == word_bits // (2 * lane_bits)
    back = LN.from_words(words, 37, fmt, lane_bits, word_bits)
    assert np.array_equal(back[0], re) and np.array_equal(back[1], im)


@pytest.mark.parametrize("word_bits", [32, 64, 128])
@pytest.mark.parametrize(
    "lane_bits,fmt",
    [
        (8, reg(8, 2)),
        (16, reg(12, 3)),
        (16, reg(16, 5)),
        (16, Format(10, 4, False)),
        (32, reg(24, 6)),
        (32, Format(32, 8, True)),
    ],
)
@pytest.mark.parametrize("n", [1, 7, 64, 1000])
def test_direct_words_are_the_general_paths(monkeypatch, word_bits, lane_bits, fmt, n):
    """The direct path (machine-width lanes: a reinterpretation of the buffer) gives the general
    ComplexField path's words, padding and shape included, and reads them back the same way."""
    if word_bits % (2 * lane_bits):
        pytest.skip("the word does not hold whole elements")
    assert LN._direct_dtype(fmt, lane_bits) is not None
    if fmt.signed:
        re, im = stored(fmt.W, n, seed=n + lane_bits)
    else:
        rng = np.random.default_rng(n)
        re, im = rng.integers(0, 1 << fmt.W, n), rng.integers(0, 1 << fmt.W, n)
    fast = LN.to_words(re, im, fmt, lane_bits, word_bits)
    back = LN.from_words(fast, n, fmt, lane_bits, word_bits)
    with monkeypatch.context() as m:
        m.setattr(LN, "_direct_dtype", lambda *a: None)
        slow = LN.to_words(re, im, fmt, lane_bits, word_bits)
        slow_back = LN.from_words(slow, n, fmt, lane_bits, word_bits)
    assert fast.dtype == slow.dtype and fast.shape == slow.shape
    assert np.array_equal(fast, slow)
    for a, b, v in zip(back, slow_back, (re, im)):
        assert a.dtype == b.dtype and np.array_equal(a, b) and np.array_equal(a, v)


def test_words_refuse_what_they_cannot_hold():
    with pytest.raises(ValueError, match="not in"):
        LN.elems_per_word(16, 48)
    with pytest.raises(ValueError, match="whole"):
        LN.elems_per_word(24, 32)
    words = LN.to_words([1], [1], Format(16, 3, True), 16, 64)  # 16 fraction-bit values
    with pytest.raises(ValueError, match="finer than"):
        LN.from_words(words, 1, reg(12, 3), 16, 64)
