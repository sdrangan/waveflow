"""The numpy fast path agrees with the canonical array serializer at every lane count.

Found by ``examples/markov`` (2026-10-04): ``to_words_numpy`` / ``from_words_numpy`` put ONE element
per word whatever the width, while the canonical serializer -- and the C++ lane routines -- pack
densely (eight ``U8`` to a 64-bit word).  It only showed when a caller passed a bare ``np.ndarray``
of a narrow type; at one lane per word the two agree, which is how it hid.  And the ``m_axi`` read
charged ``count`` words for ``count`` elements, so a narrow read moved up to 8x the words it needed.
"""
from __future__ import annotations

import numpy as np
import pytest

from waveflow.hw.arrayutils import get_nwords, read_array, write_array
from waveflow.hw.dataschema import IntField
from waveflow.hw.memif import MMIFMaster
from waveflow.simulation.simulation import Simulation

CASES = [(8, False, 64), (8, True, 32), (16, True, 64), (16, False, 32), (32, False, 64),
         (32, True, 32), (64, True, 64), (12, False, 64), (24, False, 32)]


@pytest.mark.parametrize("bits,signed,word_bw", CASES)
def test_fast_path_matches_the_serializer(bits, signed, word_bw):
    T = IntField.specialize(bitwidth=bits, signed=signed)
    n = 11
    lo, hi = (-(1 << (bits - 1)), 1 << (bits - 1)) if signed else (0, 1 << bits)
    v = np.random.default_rng(bits).integers(lo, hi, size=n).astype(T._numpy_elem_dtype())
    canon = np.asarray(write_array(v, elem_type=T, word_bw=word_bw), dtype=np.uint64)
    fast = T.to_words_numpy(v, word_bw)
    if fast is not None:
        assert np.array_equal(np.asarray(fast, dtype=np.uint64), canon)
    back = T.from_words_numpy(canon, n, word_bw)
    if back is not None:
        assert np.array_equal(np.asarray(back, dtype=np.int64), v.astype(np.int64))
    assert len(canon) == get_nwords(T, word_bw=word_bw, shape=n)


def test_an_array_read_charges_the_packed_word_count():
    U8 = IntField.specialize(bitwidth=8, signed=False)
    m = MMIFMaster(name="m", sim=Simulation(), bitwidth=64)
    assert m._typed_nwords(U8, 600, word_bw=64) == 75
    assert m._typed_nwords(U8, word_bw=64) == 1
