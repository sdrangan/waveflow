"""Two fixed-point gaps the agent-skills blind test found (``evaluation/skill_tests``).

1. ``from_real(v, T)`` returned an array of the *cached* class of T's format, not T, when T was
   specialized with ``include_dir``; ``write_uint32_file(..., elem_type=T)`` then refused it with
   "DataArray carries 'Fixed12_1' but elem_type='Fixed12_1'".  Now ``from_real`` and ``quantize``
   keep the declared class, and the file / array helpers accept any class with the same bits.
2. No comparison of fixed-point arrays at all (``a > b`` was a TypeError), and the arithmetic
   refuses mixed signedness, so a model of ``x > thresh`` (``ap_fixed`` vs ``ap_ufixed``, exact in
   C++) had to be improvised.  Now ``lt`` ... ``ne`` and ``<`` ... ``>=`` compare exactly, as
   ``ap_fixed`` does, checked here against ``fractions.Fraction``.
"""
from __future__ import annotations

from fractions import Fraction
from enum import IntEnum

import numpy as np
import pytest

from waveflow.hw import fixpoint as fx
from waveflow.hw.arrayutils import array, read_uint32_file, write_array, write_uint32_file
from waveflow.hw.dataschema import EnumField, IntField
from waveflow.hw.fixpoint import FixedField, from_real, quantize, to_real

S12 = FixedField.specialize(12, 1, include_dir="include")          # declared with include_dir
S12_CACHED = FixedField.specialize(12, 1)                            # the format's cached class
U16_4 = FixedField.specialize(16, 4, signed=False)


# --- 1. the declared class is kept, and same-bits classes are accepted ----------------------------

def test_from_real_and_quantize_keep_the_declared_class():
    assert S12 is not S12_CACHED
    x = from_real([0.5, -0.75, 0.3125], S12)
    assert type(x).element_type is S12
    assert type(quantize(x, S12)).element_type is S12
    # An op's result has a derived format: the cached class of that format, as before.
    y = x * x
    assert type(y).element_type is FixedField.specialize(24, 2)
    assert type(quantize(y, S12)).element_type is S12


def test_wrap_refuses_a_class_of_another_format():
    with pytest.raises(ValueError, match="not the result's format"):
        fx._wrap(np.zeros(2, dtype=np.int64), S12.get_format(), U16_4)


def test_file_helpers_accept_a_class_with_the_same_bits(tmp_path):
    x = from_real([0.5, -0.75, 0.3125], S12)
    p = write_uint32_file(x, elem_type=S12, file_path=tmp_path / "a.bin")      # the reported case
    cached = from_real([0.5, -0.75, 0.3125], S12_CACHED)
    q = write_uint32_file(cached, elem_type=S12, file_path=tmp_path / "b.bin")  # same bits, other class
    assert p.read_bytes() == q.read_bytes()
    back = read_uint32_file(p, elem_type=S12, shape=3)
    assert list(to_real(back)) == [0.5, -0.75, 0.3125]
    assert np.array_equal(write_array(cached, S12, word_bw=32), write_array(x, word_bw=32))


def test_file_helpers_still_refuse_different_bits(tmp_path):
    x = from_real([0.5], S12)
    with pytest.raises(TypeError, match="elem_type mismatch"):
        write_uint32_file(x, elem_type=FixedField.specialize(12, 2), file_path=tmp_path / "a.bin")
    with pytest.raises(TypeError, match="elem_type mismatch"):
        write_array(x, FixedField.specialize(12, 1, signed=False), word_bw=32)

    class Op(IntEnum):
        A = 0
        B = 1
    OpField = EnumField.specialize(enum_type=Op, bitwidth=8)
    U8 = IntField.specialize(bitwidth=8, signed=False)
    with pytest.raises(TypeError, match="elem_type mismatch"):           # an enum is not a plain int
        write_array(array(U8, [0, 1]), OpField, word_bw=32)
    U8_INC = IntField.specialize(bitwidth=8, signed=False, include_dir="include")
    assert len(write_array(array(U8, [1, 2]), U8_INC, word_bw=32)) == 1   # plain ints: same bits


# --- 2. exact comparisons ---------------------------------------------------------------------------

def _rand_stored(cls, n, rng):
    """The format's extremes, 0 and its neighbours, then *n* random stored integers."""
    f = cls.get_format()
    lo, hi = (-(1 << (f.W - 1)), (1 << (f.W - 1)) - 1) if f.signed else (0, (1 << f.W) - 1)
    vals = [lo, hi, 0, min(lo + 1, hi), max(hi - 1, lo)] + [rng.randint(lo, hi) for _ in range(n)]
    return array(cls, np.array(vals, dtype=f.dtype))


def _exact(cls, stored):
    """Each value as an exact rational: stored * 2^-F (F may be negative)."""
    frac = cls.get_format().frac_bits
    return [Fraction(int(s), 1 << frac) if frac >= 0 else Fraction(int(s) << -frac) for s in stored]


PAIRS = [
    (FixedField.specialize(12, 1), FixedField.specialize(16, 4, signed=False)),   # the pulse case
    (FixedField.specialize(8, 4), FixedField.specialize(8, 4)),
    (FixedField.specialize(8, 10), FixedField.specialize(6, -2)),                 # I > W, I < 0
    (FixedField.specialize(64, 32, signed=False), FixedField.specialize(64, 1)),  # needs > 64 bits
    (FixedField.specialize(20, 20), FixedField.specialize(3, 1, signed=False)),
]


@pytest.mark.parametrize("ca, cb", PAIRS, ids=lambda c: c.cpp_type)
def test_comparisons_are_exact(ca, cb):
    import random
    rng = random.Random(7)
    a, b = _rand_stored(ca, 40, rng), _rand_stored(cb, 40, rng)
    ra, rb = _exact(ca, a.val), _exact(cb, b.val)
    want = {
        "lt": [x < y for x, y in zip(ra, rb)], "le": [x <= y for x, y in zip(ra, rb)],
        "gt": [x > y for x, y in zip(ra, rb)], "ge": [x >= y for x, y in zip(ra, rb)],
        "eq": [x == y for x, y in zip(ra, rb)], "ne": [x != y for x, y in zip(ra, rb)],
    }
    for op, w in want.items():
        assert list(getattr(fx, op)(a, b)) == w, op
    assert list(a < b) == want["lt"] and list(a >= b) == want["ge"]
    assert sum(want["eq"]) >= 1                     # the zeros: equal values in two formats


def test_comparisons_take_both_sides_as_numpy_broadcasts():
    a = from_real([0.5, -0.75, 0.25], FixedField.specialize(12, 1))
    b = from_real([0.25], U16_4)
    assert list(fx.ge(a, b)) == [True, False, True]


def test_the_pulse_threshold_compare():
    """``x > thresh`` -- the line the blind-test agents had to improvise in Python."""
    x = from_real([0.0, 0.5, -0.75, 0.25, 0.3125], FixedField.specialize(12, 1))
    thresh = U16_4(int(0.25 * 2**12))                       # a scalar FixedField: the stored integer
    assert list(x > thresh) == [False, True, False, False, True]
    assert list(thresh < x) == [False, True, False, False, True]          # reflected
    assert list(fx.gt(x, from_real([0.25], U16_4))) == [False, True, False, False, True]


def test_a_plain_number_is_refused_and_other_kinds_are_unchanged():
    x = from_real([0.5], S12)
    with pytest.raises(TypeError, match="ambiguous"):
        x > 0.25                                                          # stored or real?
    ints = array(IntField.specialize(bitwidth=8, signed=False), [1, 2])
    with pytest.raises(TypeError):
        ints < ints                                                       # not fixed point: as before
    assert len({x, ints}) == 2                                            # still hashable
    assert (x == x) is True and (x == from_real([0.5], S12)) is False    # == is still identity
