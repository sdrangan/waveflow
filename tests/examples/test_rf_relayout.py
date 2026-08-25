"""The re-layout's toolchain-free gate: the loopback identity **and** the format in the middle.

``plans/rf_shot_buf.md`` § *The caveat, and it is a Stage A gate*.  The II and the RTL bits are
``test_rf_relayout_xsi.py``.

**Both halves are needed and neither implies the other.**  A pair of wrong-but-inverse conversions
round-trips perfectly, so the loopback says nothing about the dense format a host would have to
write; and a correct dense format says nothing about whether the inverse exists.
"""
from __future__ import annotations

import numpy as np
import pytest

from examples.rf_relayout.rf_relayout import (
    NWORD,
    WORD,
    captured_words,
    check_outputs,
    dense_golden,
    run_pysim,
    stim_codes,
    stim_words,
)


@pytest.fixture(scope="module")
def tb():
    return run_pysim()


def test_the_loopback_returns_the_stimulus(tb):
    check_outputs(captured_words(tb), where="pysim: ")
    assert captured_words(tb).size == NWORD


def test_this_configuration_is_not_the_identity(tb):
    """The measurement hazard: with equal effective and container widths there is nothing to measure."""
    assert not tb.dut.is_identity
    assert not np.array_equal(dense_golden(), stim_words()), (
        "the dense words equal the converter words — this run measured a pair of wires")


def test_the_dense_words_are_at_the_effective_stride():
    """Sample ``k`` at bit ``14k``, and eight idle bits at the top of every word."""
    dense = dense_golden()
    codes = stim_codes()
    for i in range(4):
        w = int(dense[i])
        assert w >> 56 == 0
        for k in range(4):
            got = (w >> (14 * k)) & 0x3FFF
            want = int(codes[4 * i + k]) & 0x3FFF
            assert got == want, f"word {i} sample {k}: 0x{got:04x} != 0x{want:04x}"


def test_word_zero_carries_the_four_extremes():
    """The stimulus's own claim, asserted so a later edit cannot quietly drop the hard cases.

    Full scale is where the widen-after-shift mistake shows up and nowhere else: a ramp starting at
    1000 never produces a sample whose top bits are set.
    """
    lo, hi = -8192, 8191
    assert list(stim_codes()[:4]) == [lo, hi, -1, 0]


def test_the_gated_word_is_the_4x2_s():
    assert (int(WORD.bits_per_samp), int(WORD.bits_per_samp_pack)) == (14, 16)
    assert int(WORD.justify_shift()) == 2
