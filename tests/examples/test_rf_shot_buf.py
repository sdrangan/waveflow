"""The shot buffer's toolchain-free gate: one shot in, the same shot out, phases separated.

``plans/rf_shot_buf.md`` Stage A.  The RTL half is ``test_rf_shot_buf_xsi.py``; this is the rung that
runs everywhere and is where a failure is cheapest to read.
"""
from __future__ import annotations

import numpy as np
import pytest

from examples.rf_shot_buf.rf_shot_buf import (
    NWORD,
    WORD,
    captured_words,
    check_outputs,
    run_pysim,
    shot_words,
)


@pytest.fixture(scope="module")
def tb():
    return run_pysim()


def test_the_shot_comes_back_byte_identical(tb):
    check_outputs(captured_words(tb), where="pysim: ")


def test_exactly_one_shot_was_loaded_and_played(tb):
    """Counted, because "it played *something*" is what a byte comparison alone cannot rule out."""
    assert tb.dut.n_shots == 1
    assert tb.dut.phase.n_written == NWORD
    assert tb.dut.phase.n_read == NWORD
    tb.dut.assert_phases_separated()


def test_nothing_was_dropped_at_either_boundary(tb):
    """No back-pressure loss anywhere: the shot buffer's input CAN be stalled, so nothing should be."""
    assert tb.in_if.dropped == 0
    assert tb.out_if.dropped == 0


def test_the_payload_is_real_converter_words_not_a_ramp_of_integers(tb):
    """The stimulus goes through ``pack``, so the low two bits of every slot are the converter's zeros.

    Worth asserting rather than assuming: a hand-written ramp stepping by one would be measuring the
    quantizer, and it would also stop the Stage B re-layout round trip being exact.
    """
    words = shot_words()
    shift = int(WORD.justify_shift())
    slots = np.concatenate([[(int(w) >> (16 * k)) & 0xFFFF for k in range(4)] for w in words])
    assert shift == 2
    assert np.all(slots % (1 << shift) == 0), (
        "a slot with a nonzero low bit is one no 14-bit converter could have produced")


def test_the_gated_geometry_is_the_4x2_s():
    """The recorded XSI cycle count belongs to exactly this configuration."""
    assert (int(WORD.bitwidth), int(WORD.samp_per_word)) == (64, 4)
    assert int(WORD.bits_per_samp) == 14 and int(WORD.bits_per_samp_pack) == 16


def test_a_short_load_is_reported_as_a_short_shot():
    """The named failure mode, provoked: the acceptance check must say *short*, not just "differs".

    Stage B's response exists for exactly this — a transfer that completes cleanly while the buffer
    sits half-loaded — so the diagnosis has to be legible before there is a status code to carry it.
    """
    got = captured_words(run_pysim())[:-1]
    with pytest.raises(AssertionError, match="SHORT"):
        check_outputs(got, where="pysim: ")


def test_a_rotated_shot_is_diagnosed_as_addressing():
    """The other named failure: all the payload, the wrong starting index."""
    with pytest.raises(AssertionError, match="ROTATED"):
        check_outputs(np.roll(shot_words(), 1))
