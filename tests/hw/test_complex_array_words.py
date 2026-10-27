"""A complex array whose components are machine numbers serializes as its (re, im) pairs' own bytes:
the same words, padding, dtype and shape as the lanes' shifts, and the same values read back."""

from __future__ import annotations

import numpy as np
import pytest

from waveflow.hw.complexfield import ComplexField
from waveflow.hw.dataschema import DataArray, FloatField, IntField
from waveflow.hw.fixpoint import FixedField
from waveflow.utils.complexutils import complex_dtype

DIRECT = [
    IntField.specialize(8),
    IntField.specialize(16),
    IntField.specialize(16, signed=False),
    IntField.specialize(32),
    IntField.specialize(64),
    IntField.specialize(64, signed=False),
    FixedField.specialize(16, 4),
    FixedField.specialize(32, 9, signed=False),
    FloatField.specialize(32),
    FloatField.specialize(64),
]


def values(elem: type[ComplexField], n: int, seed: int) -> np.ndarray:
    """Random stored values of ``elem``, its extremes first."""
    rng = np.random.default_rng(seed)
    if elem.kind == "float":
        v = (rng.standard_normal(n) + 1j * rng.standard_normal(n)).astype(elem._np_complex_type())
        v[: min(n, 2)] = [complex(-0.0, np.inf), complex(np.finfo(np.float32).max, -1e-30)][: min(n, 2)]
        return v
    fmt = elem.inner_format()
    lo, hi = (-(1 << (fmt.W - 1)), (1 << (fmt.W - 1)) - 1) if fmt.signed else (0, (1 << fmt.W) - 1)
    out = np.empty(n, complex_dtype(fmt))
    for part in ("re", "im"):
        out[part] = rng.integers(lo, hi, n, dtype=fmt.dtype, endpoint=True)
    out["re"][: min(n, 2)], out["im"][: min(n, 2)] = (lo, hi)[: min(n, 2)], (hi, lo)[: min(n, 2)]
    return out


def lanes_path(monkeypatch):
    """Switch the byte path off: the array then takes its lanes' shifts."""
    monkeypatch.setattr(ComplexField, "_words_numpy", classmethod(lambda cls, *a: None))
    monkeypatch.setattr(ComplexField, "_from_words_numpy", classmethod(lambda cls, *a: None))


@pytest.mark.parametrize("inner", DIRECT, ids=lambda t: t.__name__)
@pytest.mark.parametrize("word_bw", [32, 64, 128, 256])
@pytest.mark.parametrize("n", [1, 5, 64])
def test_direct_words_are_the_lanes(monkeypatch, inner, word_bw, n):
    elem = ComplexField.specialize(inner)
    if word_bw % elem.get_bitwidth():
        pytest.skip("the word does not hold whole elements")
    arr_t = DataArray.specialize(elem, max_shape=(n,))
    v = values(elem, n, seed=n + word_bw)
    assert elem._part_dtype(word_bw) is not None              # the byte path is taken
    fast = arr_t(v).serialize(word_bw)
    back = arr_t().deserialize(fast, word_bw).val
    with monkeypatch.context() as m:
        lanes_path(m)
        slow = arr_t(v).serialize(word_bw)
        slow_back = arr_t().deserialize(slow, word_bw).val
    assert fast.dtype == slow.dtype and fast.shape == slow.shape
    assert np.array_equal(fast, slow)
    assert back.dtype == slow_back.dtype == v.dtype
    assert back.tobytes() == slow_back.tobytes() == v.tobytes()   # bit for bit, NaN and -0.0 too


def test_other_widths_take_the_lanes():
    """A 12-bit component is not a machine number, a 96-bit word not whole 64-bit chunks."""
    assert ComplexField.specialize(FixedField.specialize(12, 3))._part_dtype(96) is None
    elem = ComplexField.specialize(IntField.specialize(16))
    assert elem._part_dtype(96) is None and elem._part_dtype(128) is not None
    assert ComplexField.specialize(FloatField.specialize(32))._part_dtype(32) is None


def test_reads_drop_bits_above_the_word():
    """A 32-bit word handed over as a wider integer reads as its low 32 bits, as the lanes do."""
    elem = ComplexField.specialize(IntField.specialize(16))
    arr_t = DataArray.specialize(elem, max_shape=(3,))
    v = values(elem, 3, seed=1)
    words = arr_t(v).serialize(32).astype(np.uint64) | np.uint64(0xABCD << 40)
    assert arr_t().deserialize(words, 32).val.tobytes() == v.tobytes()
