"""The logic-side re-layout as a *module*: the twins, the round trips, and the identity trap.

``plans/rf_shot_buf.md`` § *The logic-side port*.  The II is a csynth measurement and lives in
``tests/examples/test_rf_relayout_xsi.py``; what is here is everything about the conversion that a
toolchain is not needed for.

**The identity trap has its own test, and it is the reason this module exists.**  When
``bits_per_samp == bits_per_samp_pack`` the whole conversion is a pair of wires — which is every
configuration in this repo but the RFSoC 4x2 preset — so a gate could be green while measuring
nothing at all.
"""
from __future__ import annotations

import numpy as np
import pytest

from waveflow.build.codegen_check import check
from waveflow.hw.rf_relayout import (
    RfRelayout,
    check_geometry,
    dense_elem_type,
    slot_elem_type,
    slots_per_word,
    to_dense,
    to_slots,
)
from waveflow.hw.rfdc_samp_word import RfdcSampWord, Rfsoc4x2SampWord, pack
from waveflow.simulation.simulation import Simulation

#: The word this whole exercise is about: 14 effective bits in a 16-bit slot, four to a 64-bit beat.
W4X2 = Rfsoc4x2SampWord.specialize(samp_per_word=4)
#: The word that makes the conversion a no-op — every other configuration in the repo.
W_FLAT = RfdcSampWord.specialize(samp_per_word=4, bits_per_samp=16, bits_per_samp_pack=16)


def _words(word, codes) -> np.ndarray:
    return np.asarray(pack(word, np.asarray(codes, dtype=np.int64).reshape(1, -1)),
                      dtype=np.uint64).ravel()


# ---------------------------------------------------------------------------
# The element types the C++ serializes through
# ---------------------------------------------------------------------------

def test_the_two_element_types_carry_fixed_names_and_the_word_s_widths():
    """The names are what the generated headers are called; the widths come from the converter.

    That split is what lets a *framework* task body write ``#include "rf_slot_elem_array_utils.h"``
    without knowing 14 or 16.
    """
    assert slot_elem_type(W4X2).__name__ == "RfSlotElem"
    assert dense_elem_type(W4X2).__name__ == "RfDenseElem"
    assert slot_elem_type(W4X2).get_bitwidth() == 16
    assert dense_elem_type(W4X2).get_bitwidth() == 14
    assert slot_elem_type(W4X2).signed and dense_elem_type(W4X2).signed, (
        "both are signed: a justified sample read back as a plain integer has to sign-extend the "
        "way the RTL's ap_int does")


def test_the_element_types_are_cached_so_codegen_sees_one_class():
    assert slot_elem_type(W4X2) is slot_elem_type(W4X2)
    assert slot_elem_type(W4X2) is not dense_elem_type(W4X2)


def test_slots_per_word_doubles_for_interleaved_iq():
    iq = Rfsoc4x2SampWord.specialize(samp_per_word=2, iq_mode=True)
    assert slots_per_word(W4X2) == 4
    assert slots_per_word(iq) == 4, "two complex samples are four slots"


# ---------------------------------------------------------------------------
# The conversion
# ---------------------------------------------------------------------------

def test_the_dense_layout_is_what_the_serializer_emits_at_the_effective_stride():
    """Slot 3 lands at bit 42, not at bit 48 — the whole claim of "densely packed".

    Checked against the bits rather than against a second implementation: a 14-bit element at 14-bit
    stride puts sample ``k`` at ``[14k+13 : 14k]``, and the top 8 bits of the 64-bit word are idle.
    """
    codes = np.array([1, 2, 3, 4], dtype=np.int64)
    dense = int(to_dense(W4X2, _words(W4X2, codes))[0])
    for k, c in enumerate(codes):
        assert (dense >> (14 * k)) & 0x3FFF == int(c), f"sample {k} is not at bit {14 * k}"
    assert dense >> 56 == 0, "the top 8 bits of a dense-14 64-bit word are idle"


def test_dense_then_slots_is_the_identity_for_a_properly_justified_word():
    """The direction the RTL loopback grades, and the condition it holds under.

    A left-justified 14-in-16 slot has two low bits the converter never sets; building the stimulus
    through ``pack`` from *codes* is what makes that automatic.
    """
    codes = np.arange(-8192, -8192 + 64, dtype=np.int64)
    words = _words(W4X2, codes)
    assert np.array_equal(to_slots(W4X2, to_dense(W4X2, words)), words)


def test_slots_then_dense_is_the_identity_unconditionally():
    """The other order discards nothing, so it needs no precondition at all."""
    rng = np.random.default_rng(0)
    codes = rng.integers(-8192, 8192, size=64, dtype=np.int64)
    dense = to_dense(W4X2, _words(W4X2, codes))
    assert np.array_equal(to_dense(W4X2, to_slots(W4X2, dense)), dense)


def test_the_extremes_survive_both_directions():
    """Full scale is where a shift performed in the wrong WIDTH shows up, and only there.

    A sample shifted up inside the narrow dense type loses its top bits — a full-scale value comes
    back small, which is a signal-level error rather than a crash and which a ramp that never
    approaches full scale would pass.
    """
    lo, hi = -8192, 8191
    codes = np.array([lo, hi, -1, 0], dtype=np.int64)
    words = _words(W4X2, codes)
    assert np.array_equal(to_slots(W4X2, to_dense(W4X2, words)), words)
    dense = int(to_dense(W4X2, words)[0])
    assert (dense & 0x3FFF) == (lo & 0x3FFF) and ((dense >> 14) & 0x3FFF) == hi


def test_negative_samples_survive_the_arithmetic_shift():
    codes = np.array([-1, -4096, -8192, 8191], dtype=np.int64)
    words = _words(W4X2, codes)
    assert np.array_equal(to_slots(W4X2, to_dense(W4X2, words)), words)


# ---------------------------------------------------------------------------
# The identity trap
# ---------------------------------------------------------------------------

def test_a_flat_word_makes_the_conversion_the_identity():
    """The measurement hazard, named: with equal widths there is nothing to measure.

    This is not a defect of the design — it is the reason ``examples/rf_relayout`` is built on the
    4x2 preset and refuses to elaborate at ``shift == 0``.
    """
    words = _words(W_FLAT, np.arange(4, dtype=np.int64))
    assert W_FLAT.justify_shift() == 0
    assert np.array_equal(to_dense(W_FLAT, words), words)
    flat = RfRelayout.for_word(W_FLAT, sim=Simulation(), name="flat")
    assert flat.is_identity


def test_the_gated_configuration_is_not_the_identity():
    live = RfRelayout.for_word(W4X2, sim=Simulation(), name="live")
    assert not live.is_identity
    assert (live.bitwidth, live.n_slot, live.shift) == (64, 4, 2)


def test_a_word_whose_dense_form_does_not_fit_is_refused():
    """The dense side must fit the SAME word — that is what makes this a re-layout, not a conversion."""
    bad = RfdcSampWord.specialize(samp_per_word=4, bits_per_samp=16, bits_per_samp_pack=16)
    check_geometry(bad)                                  # 4 x 16 = 64: fits exactly
    class _Fake:                                         # a word type that lies about its width
        __name__ = "FakeWord"
        bits_per_samp, bitwidth = 16, 32
        @staticmethod
        def slots_per_word() -> int:
            return 4
    with pytest.raises(ValueError, match="mis-specialized"):
        check_geometry(_Fake)


# ---------------------------------------------------------------------------
# Codegen
# ---------------------------------------------------------------------------

def test_the_pair_lowers_to_a_free_running_kernel():
    ok, msg = check(RfRelayout, "composite_kernel")
    assert ok, msg


def test_the_template_args_are_the_three_integers_the_bodies_need():
    r = RfRelayout.for_word(W4X2, sim=Simulation(), name="r")
    assert r.to_dense.kernel_task().template_args == (64, 4, 2)
    assert r.to_slots.kernel_task().template_args == (64, 4, 2)
    from waveflow.build.composite_gen import _unpack_boundary
    assert [_unpack_boundary(e)[0] for e in r.boundary] == ["s_in", "s_out"]
