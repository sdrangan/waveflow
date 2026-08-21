"""The RFDC packing convention, as a type — :mod:`waveflow.hw.rfdc_samp_word`.

Three things are checked here and nowhere else.

**The geometry arithmetic**, including the effective/container split that this type exists to make
expressible: ``bits_per_samp`` sets the quantizer, ``bits_per_samp_pack`` sets the bus, and they are
allowed to differ.

**``justify``** — the round trip through a wider container, in both alignments.  The *value* of the
default is an assumption awaiting a lab measurement (see the class docstring); what is tested is that
whichever one is declared is what the packing does.

**``iq_order`` at two samples per word.**  Slot order is unobservable at one sample per word — the
standing trap in this repo — so a test written at ``samp_per_word == 1`` proves nothing about it.  It
is pinned here at two, and this is the only place the rule is stated in a runnable form.

The word type also has to remain an ordinary ``DataSchema`` element, because "a block of words is a
``DataArray`` over the word type" is the whole reason it subclasses ``IntField`` rather than inventing
a parallel hierarchy.  That is checked by round-tripping a block through the real serializers.
"""
from __future__ import annotations

import inspect
from pathlib import Path

import numpy as np
import pytest

from waveflow.hw.arrayutils import read_array, write_array
from waveflow.hw.dataschema import DataArray, IntField
from waveflow.hw.fixpoint import from_real, to_real
from waveflow.hw.rfdc_samp_word import Rfsoc4x2SampWord, RfdcSampWord


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------

class TestGeometry:

    @pytest.mark.parametrize("spw,eff,pack,iq,want", [
        (1, 16, 16, False, 16),
        (4, 16, 16, False, 64),
        (4, 14, 16, False, 64),      # 14-in-16 does NOT change the bus width
        (4, 12, 12, False, 48),
        (2, 16, 16, True, 64),       # a complex sample is two slots
        (8, 16, 16, True, 256),      # over 64 bits: the WORD type has no such ceiling
    ])
    def test_the_word_width_follows_from_the_container_not_the_converter(self, spw, eff, pack, iq,
                                                                         want):
        """``samp_per_word * bits_per_samp_pack``, doubled for I/Q.

        The **container** is what decides the bus, and that is the half of the old ``nbits`` this
        keeps.  Row 3 is the point of the type: dropping the effective width to 14 leaves the word at
        64 bits, so the bus arithmetic no longer drags the quantizer with it.
        """
        W = RfdcSampWord.specialize(samp_per_word=spw, bits_per_samp=eff,
                                    bits_per_samp_pack=pack, iq_mode=iq)
        assert W.bitwidth == want
        assert W.slots_per_word() == spw * (2 if iq else 1)

    def test_the_quantizer_takes_the_EFFECTIVE_width(self):
        """The one substitution the whole type is for.

        A 14-bit converter on a 16-bit bus quantizes to 14.  Reading the container here is the defect
        that was latent while one ``nbits`` meant both numbers, and it is silent: the model simply
        reports quantization noise four times finer than the hardware's.
        """
        W = RfdcSampWord.specialize(samp_per_word=4, bits_per_samp=14, bits_per_samp_pack=16)
        assert W.samp_type().bitwidth == 14
        assert W.samp_type().cpp_type == "ap_fixed<14, 1, AP_RND, AP_SAT>"
        assert W.slot_type().bitwidth == 16
        assert W.bitwidth == 64

    def test_a_container_narrower_than_its_contents_is_refused(self):
        with pytest.raises(ValueError, match="narrower than"):
            RfdcSampWord.specialize(bits_per_samp=16, bits_per_samp_pack=14)

    @pytest.mark.parametrize("kwargs,match", [
        (dict(samp_per_word=0), "at least 1"),
        (dict(bits_per_samp=1), "at least 2"),
        (dict(justify="middle"), "justify must be one of"),
        (dict(iq_order="i_high"), "iq_order must be one of"),
    ])
    def test_nonsense_is_refused_at_specialization(self, kwargs, match):
        with pytest.raises(ValueError, match=match):
            RfdcSampWord.specialize(**kwargs)

    def test_specializing_is_cached_so_two_asks_are_the_same_type(self):
        """Element types are compared by identity all over the serializers."""
        a = RfdcSampWord.specialize(samp_per_word=4)
        b = RfdcSampWord.specialize(samp_per_word=4)
        assert a is b


# ---------------------------------------------------------------------------
# Presets
# ---------------------------------------------------------------------------

class TestPreset:

    def test_a_preset_is_a_subclass_and_specializing_it_keeps_its_numbers(self):
        """A board preset restates only what the board fixes; everything else is inherited.

        That is why ``specialize`` takes ``None`` as "keep what ``cls`` says" rather than falling
        back to the base class's defaults: a preset that lost its own numbers the moment you asked
        for a different ``samp_per_word`` would be a factory function wearing a class's clothes.
        """
        W = Rfsoc4x2SampWord.specialize(samp_per_word=4)
        assert issubclass(W, Rfsoc4x2SampWord)
        assert W.samp_per_word == 4
        assert W.bits_per_samp == Rfsoc4x2SampWord.bits_per_samp
        assert W.bits_per_samp_pack == Rfsoc4x2SampWord.bits_per_samp_pack

    def test_the_4x2_slot_is_16_bits(self):
        """The **container** half of the board's answer, and the half that is not in doubt.

        The AXI-Stream slot width is 16 bits on a ZU48DR whatever the converter resolves, so this is
        the number the bus arithmetic uses and it is stable across the effective-width question.
        """
        assert Rfsoc4x2SampWord.bits_per_samp_pack == 16
        assert Rfsoc4x2SampWord.specialize(samp_per_word=4).bitwidth == 64


# ---------------------------------------------------------------------------
# justify
# ---------------------------------------------------------------------------

class TestJustify:

    def test_the_default_is_left_and_is_flagged_unconfirmed_in_the_docstring(self):
        """The default is an **assumption**, and a reader must be able to tell.

        Whether AMD's RFDC is MSB- or LSB-aligned is a PG269 question nobody here has answered; it
        will be settled in the lab.  This test does not assert the answer — it asserts that the code
        says it does not know, which is the property that must survive until a measurement replaces
        it.
        """
        assert RfdcSampWord.justify == "left"
        # The warning lives on the FIELD, which is where a reader looking up `justify` lands, so it
        # is the source that has to carry it rather than the class docstring alone.
        src = Path(inspect.getfile(RfdcSampWord)).read_text(encoding="utf-8")
        assert "UNCONFIRMED" in src, "the justify default must be marked unconfirmed in the source"
        assert "PG269" in src and "lab" in src
        assert "assumption, not a measurement" in (RfdcSampWord.__doc__ or "")
        # ...and in the page a reader is sent to.
        page = (Path(__file__).resolve().parents[2]
                / "docs" / "guide" / "rf" / "rfdc" / "axis_side.md").read_text(encoding="utf-8")
        assert "not yet confirmed" in page and "PG269" in page

    def test_left_justification_puts_the_effective_bits_at_the_top_of_the_slot(self):
        W = RfdcSampWord.specialize(samp_per_word=4, bits_per_samp=14, bits_per_samp_pack=16,
                                    justify="left")
        assert W.justify_shift() == 2
        assert list(W.to_slots(np.array([1, -1, 8191, -8192]))) == [4, -4, 32764, -32768]

    def test_right_justification_leaves_the_value_alone(self):
        W = RfdcSampWord.specialize(samp_per_word=4, bits_per_samp=14, bits_per_samp_pack=16,
                                    justify="right")
        assert W.justify_shift() == 0
        assert list(W.to_slots(np.array([1, -1, 8191, -8192]))) == [1, -1, 8191, -8192]

    @pytest.mark.parametrize("justify", ["left", "right"])
    def test_the_stored_value_survives_the_round_trip_in_both_alignments(self, justify):
        """Including the negatives, which is where an unsigned shift would go wrong."""
        W = RfdcSampWord.specialize(samp_per_word=4, bits_per_samp=14, bits_per_samp_pack=16,
                                    justify=justify)
        stored = np.array([0, 1, -1, 4095, -4096, 8191, -8192])
        assert list(W.from_slots(W.to_slots(stored))) == list(stored)

    def test_justification_is_a_no_op_when_the_widths_agree(self):
        """Which is why every configuration that predates the split is expressed unchanged."""
        W = RfdcSampWord.specialize(samp_per_word=4, bits_per_samp=16, bits_per_samp_pack=16)
        assert W.justify_shift() == 0
        stored = np.array([0, 1, -1, 32767, -32768])
        assert list(W.to_slots(stored)) == list(stored)


# ---------------------------------------------------------------------------
# iq_order — the rule that is invisible at one sample per word
# ---------------------------------------------------------------------------

class TestIqOrder:

    def test_i_low_puts_I_in_the_lower_slot_at_two_samples_per_word(self):
        """**Two** samples per word, deliberately.

        At one the interleaved sequence is ``[I, Q]`` under either rule and the test proves nothing.
        Two complex samples give ``[I0, Q0, I1, Q1]`` against ``[Q0, I0, Q1, I1]``, which the two
        rules actually disagree about.
        """
        W = RfdcSampWord.specialize(samp_per_word=2, iq_mode=True, iq_order="i_low")
        assert list(W.iq_interleave([1, 2], [10, 20])) == [1, 10, 2, 20]

    def test_q_low_is_the_other_answer_and_is_visibly_different(self):
        W = RfdcSampWord.specialize(samp_per_word=2, iq_mode=True, iq_order="q_low")
        assert list(W.iq_interleave([1, 2], [10, 20])) == [10, 1, 20, 2]

    @pytest.mark.parametrize("order", ["i_low", "q_low"])
    def test_deinterleave_inverts_interleave(self, order):
        W = RfdcSampWord.specialize(samp_per_word=2, iq_mode=True, iq_order=order)
        i, q = np.array([1, 2, 3, 4]), np.array([-1, -2, -3, -4])
        gi, gq = W.iq_deinterleave(W.iq_interleave(i, q))
        assert list(gi) == list(i) and list(gq) == list(q)

    def test_the_order_reaches_the_WORD_and_not_just_the_sequence(self):
        """The rule has to survive the serializer, which is where it would actually be used.

        Packing ``[I0, Q0]`` into one 64-bit beat of four 16-bit slots puts I0 in the least
        significant slot under ``i_low`` — the same "oldest in the lowest slot" convention the real
        samples follow, with I counting as older than its own Q.
        """
        W = RfdcSampWord.specialize(samp_per_word=2, iq_mode=True, iq_order="i_low")
        slots = W.iq_interleave([0x0111, 0x0222], [0x0333, 0x0444])
        word = write_array(np.asarray(slots), elem_type=W.slot_type(), word_bw=W.bitwidth)
        assert int(np.asarray(word).ravel()[0]) == 0x0444_0222_0333_0111

    def test_asking_a_real_word_for_an_IQ_rule_is_refused(self):
        W = RfdcSampWord.specialize(samp_per_word=2)
        with pytest.raises(ValueError, match="real-sample word"):
            W.iq_interleave([1], [2])


# ---------------------------------------------------------------------------
# It is an ordinary schema element — blocks compose, nothing is built in
# ---------------------------------------------------------------------------

class TestItIsAnOrdinaryElement:

    def test_a_block_of_words_is_a_DataArray_and_needs_no_new_machinery(self):
        """"Blocks compose; do not build them in."  This is that claim, run."""
        W = Rfsoc4x2SampWord.specialize(samp_per_word=4)
        Blk = DataArray.specialize(element_type=W, max_shape=(8,), static=True)
        assert Blk.get_bitwidth() == 8 * W.bitwidth
        blk = Blk()
        assert isinstance(blk.val, np.ndarray)

    def test_a_word_wider_than_64_bits_serializes_on_the_documented_convention(self):
        """``bitwidth > 64`` is ``(n, k)`` uint64, little-endian — already the rule, already handled.

        Checked here because the word type is the first thing in the RF stack that can *ask* for it:
        eight complex 16-bit samples is a 256-bit beat.  The serializers take it; the *wrappers*
        above them (``rf_samp_buf.unpack_samples``) still assume one uint64 per word, and the
        converter refuses over 64 bits, so nothing reaches that path today.
        """
        W = RfdcSampWord.specialize(samp_per_word=8, iq_mode=True)
        assert W.bitwidth == 256
        vals = np.arange(16, dtype=np.int64) - 8
        packed = np.asarray(write_array(vals, elem_type=W.slot_type(), word_bw=W.bitwidth))
        assert packed.shape == (1, 4)
        back = read_array(packed, elem_type=W.slot_type(), word_bw=W.bitwidth, shape=16)
        assert list(np.asarray(back.val).ravel()) == list(vals)

    def test_the_samples_a_word_carries_round_trip_through_the_real_serializers(self):
        """End to end at the shape ``Rfdc`` uses: real -> quantize -> justify -> pack -> back.

        Never a hand-rolled shift: the word<->slot step is ``write_array``/``read_array``, and the
        only arithmetic the type adds is the justification, which is the rule they cannot know.
        """
        W = RfdcSampWord.specialize(samp_per_word=4, bits_per_samp=14, bits_per_samp_pack=16)
        x = np.array([0.0, 0.25, -0.25, 0.9999], dtype=np.float64)
        stored = from_real(x, W.samp_type())
        words = write_array(W.to_slots(stored), elem_type=W.slot_type(), word_bw=W.bitwidth)
        assert np.asarray(words).ravel().size == 1          # four samples, one beat

        slots = read_array(np.asarray(words), elem_type=W.slot_type(), word_bw=W.bitwidth, shape=4)
        got = DataArray.specialize(element_type=W.samp_type(), max_shape=(4,), static=True)()
        got.val = W.from_slots(slots)
        assert np.allclose(to_real(got), to_real(stored))
        assert isinstance(W.slot_type()(), IntField)
