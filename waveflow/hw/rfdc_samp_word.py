"""rfdc_samp_word.py — :class:`RfdcSampWord`, the AMD RFDC packing convention as a **type**.

One AXI-Stream beat off (or into) an RF data converter is not "an integer of some width": it is
``samp_per_word`` samples laid out by a convention the *vendor* fixes.  Before this type those rules
lived as three loose parameters on ``Rfdc`` plus prose in ``docs/guide/rf/rfdc/axis_side.md``, and
one of them was doing double duty — see below.  Naming the convention puts it in one place, names it
after the block whose convention it is, and makes the two rules a serializer cannot know
(:attr:`~RfdcSampWord.justify`, :attr:`~RfdcSampWord.iq_order`) into declarations that a lab
measurement can contradict.

The defect it fixes: ``nbits`` meant two numbers
------------------------------------------------
``Rfdc`` computed both of these from a single ``nbits``::

    axis_bitwidth = samp_per_word * nbits * (2 if iq_mode else 1)   # the CONTAINER width
    SampType      = FixedField.specialize(nbits, 1, signed=True)    # the QUANTIZER precision

On a Gen 3 RFSoC those are **different numbers**.  The ZU48DR's converters resolve **14** bits and
the AXI-Stream carries them in **16**-bit slots.  Set ``nbits = 16`` to match the bus — which is what
the bus arithmetic tells you to do — and the quantizer silently became 16-bit too: four times finer
than the hardware, understating quantization noise, which is the one effect this model exists to
reproduce bit-exactly.  Here the two are :attr:`~RfdcSampWord.bits_per_samp` (effective) and
:attr:`~RfdcSampWord.bits_per_samp_pack` (container), and they are allowed to differ.

It is **not new packing machinery**
-----------------------------------
``write_array`` / ``read_array`` already take ``(elem_type, word_bw)`` and do the word<->element
work, including the ``> 64``-bit multi-word convention documented at the top of
:mod:`waveflow.hw.interface`.  This type *supplies* them: :meth:`~RfdcSampWord.samp_type` is the
quantizer, :meth:`~RfdcSampWord.slot_type` is the container element, and :attr:`bitwidth` is the word
width.  The only arithmetic here is the **justification shift** between the two, which is a rule
about where the effective bits sit inside a slot — a question no serializer can answer.

Subclass to preset
------------------
:meth:`RfdcSampWord.specialize` inherits every field it is not given from ``cls``, so a board preset
is an ordinary subclass that restates only what the board fixes — see :class:`Rfsoc4x2SampWord`.
"""
from __future__ import annotations

from typing import Any, ClassVar

import numpy as np

from waveflow.hw.dataschema import IntField
from waveflow.hw.fixpoint import FixedField
from waveflow.utils.fixputils import OMode, QMode

#: The two answers to "where do the effective bits sit inside the container slot".
JUSTIFY = ("left", "right")

#: The two answers to "which of I and Q takes the lower slot".
IQ_ORDER = ("i_low", "q_low")


class RfdcSampWord(IntField):
    """One AXI-Stream beat of converter samples, as a schema type.

    A **word**, not a sample: a word is what one beat carries.  The sample stays a
    :class:`~waveflow.hw.fixpoint.FixedField`, so the quantizer is unchanged and remains the
    integer-backed, bit-exact one — this type only says how many of them ride a beat, how wide the
    slot each one occupies is, and where inside that slot the effective bits sit.

    An unsigned :class:`~waveflow.hw.dataschema.IntField` underneath, because a beat *is* a bag of
    bits and this class never interprets one arithmetically.  That inheritance is also what makes a
    **block of words** an ordinary ``DataArray`` over this type, with the C++ serializers that
    already exist — nothing here builds a block.

    Fields
    ------
    Every one is a ``ClassVar`` set by :meth:`specialize`; instances carry only ``.val``.

    Warning
    -------
    :attr:`justify` is **an assumption, not a measurement** — see its own documentation.
    """

    #: Samples carried by one beat.  When :attr:`iq_mode` is set these are **complex** samples, so a
    #: beat is twice as wide for the same count (an (I, Q) pair occupies two slots).
    samp_per_word: ClassVar[int] = 1
    #: **Effective** bits — what the converter actually resolves, and the quantizer's precision.
    #: This is the number that decides how coarse the model's quantization noise is.
    bits_per_samp: ClassVar[int] = 16
    #: **Container** bits — the width of the slot one sample occupies on the bus.  Equal to
    #: :attr:`bits_per_samp` on a part whose resolution happens to match its slot width, and larger
    #: on one whose does not (14-in-16 on a ZU48DR).
    bits_per_samp_pack: ClassVar[int] = 16
    #: ``False`` = real samples, ``True`` = interleaved I/Q.  A statement about **packing**, which is
    #: why it lives on the word rather than on the converter: it is what makes :attr:`bitwidth`
    #: follow from the type instead of from a flag somewhere else.
    iq_mode: ClassVar[bool] = False
    #: Where the :attr:`bits_per_samp` effective bits sit inside the :attr:`bits_per_samp_pack`
    #: container slot — ``"left"`` (MSB-aligned, low bits zero) or ``"right"`` (LSB-aligned,
    #: sign-extended into the high bits).
    #:
    #: .. warning::
    #:
    #:    **The default is UNCONFIRMED.**  Which one AMD's RFDC uses is a PG269 question nobody on
    #:    this project has answered; it is on the board bring-up log beside the ``TVALID`` question
    #:    and will be settled in the lab.  ``"left"`` is the default because MSB alignment makes
    #:    full scale the same integer whatever the converter's resolution, so PL logic need not be
    #:    re-scaled per part — a reason to *expect* it, not evidence that it is so.  It is a declared
    #:    field precisely so the model **states** an answer that hardware can contradict, instead of
    #:    assuming one silently.  It is a no-op while ``bits_per_samp == bits_per_samp_pack``.
    justify: ClassVar[str] = "left"
    #: Which of I and Q takes the **lower** slot when :attr:`iq_mode` is set.  **Invisible at
    #: ``samp_per_word == 1``** — the standing trap in this repo — so it is pinned by a test at two
    #: samples per word and nowhere else.
    iq_order: ClassVar[str] = "i_low"

    #: A beat is a bag of bits; nothing here reads one as a number.
    signed: ClassVar[bool] = False
    _specializations: ClassVar[dict[tuple[Any, ...], type["RfdcSampWord"]]] = {}

    bitwidth: ClassVar[int] = 16
    cpp_type: ClassVar[str] = "ap_uint<16>"

    # -- construction ----------------------------------------------------------------------------

    @classmethod
    def specialize(  # type: ignore[override]
        cls,
        samp_per_word: int | None = None,
        bits_per_samp: int | None = None,
        bits_per_samp_pack: int | None = None,
        iq_mode: bool | None = None,
        justify: str | None = None,
        iq_order: str | None = None,
        **kwargs: Any,
    ) -> type["RfdcSampWord"]:
        """Return a cached word type.  **Anything omitted is inherited from ``cls``.**

        That inheritance is what makes a board preset an ordinary subclass rather than a factory
        function: :class:`Rfsoc4x2SampWord` restates only the two numbers the board fixes, and
        ``Rfsoc4x2SampWord.specialize(samp_per_word=4)`` keeps them.
        """
        spw = int(cls.samp_per_word if samp_per_word is None else samp_per_word)
        eff = int(cls.bits_per_samp if bits_per_samp is None else bits_per_samp)
        cont = int(cls.bits_per_samp_pack if bits_per_samp_pack is None else bits_per_samp_pack)
        iq = bool(cls.iq_mode if iq_mode is None else iq_mode)
        just = str(cls.justify if justify is None else justify)
        order = str(cls.iq_order if iq_order is None else iq_order)

        if spw < 1:
            raise ValueError(f"samp_per_word must be at least 1, got {spw}.")
        if eff < 2:
            raise ValueError(
                f"bits_per_samp must be at least 2 (a signed sample needs a sign bit and a "
                f"magnitude bit), got {eff}.")
        if cont < eff:
            raise ValueError(
                f"bits_per_samp_pack={cont} is narrower than bits_per_samp={eff}: the container "
                f"cannot be smaller than what it contains. bits_per_samp is what the converter "
                f"RESOLVES; bits_per_samp_pack is the slot it rides in.")
        if just not in JUSTIFY:
            raise ValueError(f"justify must be one of {JUSTIFY}, got {just!r}.")
        if order not in IQ_ORDER:
            raise ValueError(f"iq_order must be one of {IQ_ORDER}, got {order!r}.")

        overrides = cls.validate_specialize_kwargs(kwargs)
        override_items = tuple(sorted(overrides.items()))
        key = (cls, spw, eff, cont, iq, just, order, override_items)
        cached = cls._specializations.get(key)
        if cached is not None:
            return cached

        bw = spw * cont * (2 if iq else 1)
        subclass_name = (f"RfdcSampWord{spw}x{eff}" + (f"in{cont}" if cont != eff else "")
                         + ("_iq" if iq else ""))
        attrs = cls.merge_specialize_attrs(
            {
                "samp_per_word": spw, "bits_per_samp": eff, "bits_per_samp_pack": cont,
                "iq_mode": iq, "justify": just, "iq_order": order,
                "bitwidth": bw, "signed": False, "cpp_type": f"ap_uint<{bw}>",
                "__module__": cls.__module__,
                "__doc__": (
                    f"{spw} {'complex' if iq else 'real'} sample(s) per beat, {eff} effective bits "
                    f"in a {cont}-bit slot, {just}-justified: a {bw}-bit word."),
            },
            overrides,
        )
        specialized = type(subclass_name, (cls,), attrs)
        cls._specializations[key] = specialized
        return specialized

    # -- the two element types ---------------------------------------------------------------

    @classmethod
    def samp_type(cls) -> type[FixedField]:
        """The type one sample is **quantized** to: ``ap_fixed<bits_per_samp, 1>`` over [-1, 1),
        rounding and **saturating** — a converter clips, it does not wrap.

        Integer-backed, so it is bit-exact with the Vitis type rather than a float approximation of
        it.  Its width is :attr:`bits_per_samp`, the **effective** count, and that single
        substitution is the defect this type exists to fix.
        """
        return FixedField.specialize(int(cls.bits_per_samp), 1, signed=True,
                                     q_mode=QMode.AP_RND, o_mode=OMode.AP_SAT)

    @classmethod
    def slot_type(cls) -> type[IntField]:
        """The **container** element handed to ``write_array`` / ``read_array`` — one slot of
        :attr:`bits_per_samp_pack` bits, signed two's complement.

        Signed because a justified sample read back as a plain integer must sign-extend the way the
        RTL's ``ap_int`` does.  This is the type that makes a word ``samp_per_word`` (or twice that,
        interleaved) contiguous slots, and it is the only thing the serializers ever see.
        """
        return IntField.specialize(bitwidth=int(cls.bits_per_samp_pack), signed=True)

    @classmethod
    def slots_per_word(cls) -> int:
        """Container slots one beat carries — :attr:`samp_per_word`, doubled for interleaved I/Q."""
        return int(cls.samp_per_word) * (2 if cls.iq_mode else 1)

    # -- the one rule the serializers cannot know: justification ---------------------------------

    @classmethod
    def justify_shift(cls) -> int:
        """Bits the effective value is shifted left by to sit in its slot.

        ``bits_per_samp_pack - bits_per_samp`` when :attr:`justify` is ``"left"``, else ``0``.  Zero
        whenever the two widths agree, which is why every configuration that predates the
        effective/container split is expressed unchanged.
        """
        return (int(cls.bits_per_samp_pack) - int(cls.bits_per_samp)
                if cls.justify == "left" else 0)

    @classmethod
    def to_slots(cls, stored: Any) -> np.ndarray:
        """Stored **effective** integers -> the signed container-slot values a word is made of.

        ``stored`` is what :func:`~waveflow.hw.fixpoint.from_real` produced against
        :meth:`samp_type` — integers in ``[-2**(eff-1), 2**(eff-1) - 1]``.  The result is fed to the
        generated array serializer with :meth:`slot_type`; the shift here is the *only* bit
        manipulation this module does, and it is a justification rule, not word packing.
        """
        vals = np.asarray(getattr(stored, "val", stored), dtype=np.int64).ravel()
        return vals << cls.justify_shift()

    @classmethod
    def from_slots(cls, slots: Any) -> np.ndarray:
        """The inverse of :meth:`to_slots` — container-slot values back to stored effective ones.

        An **arithmetic** right shift (numpy's ``>>`` on a signed dtype is arithmetic), so a negative
        sample survives the round trip.  Bits below the shift are discarded, which is exactly what
        the hardware does with them.
        """
        vals = np.asarray(getattr(slots, "val", slots), dtype=np.int64).ravel()
        return vals >> cls.justify_shift()

    # -- the other rule: I/Q slot order ----------------------------------------------------------

    @classmethod
    def iq_interleave(cls, i_stored: Any, q_stored: Any) -> np.ndarray:
        """Two stored streams -> one slot sequence, ordered by :attr:`iq_order`.

        ``"i_low"`` puts I in the **lower** (earlier, less significant) slot of each pair.  The
        distinction is unobservable at one sample per word — the reason it is a declared field and
        the reason its test is written at two.
        """
        if not cls.iq_mode:
            raise ValueError(
                f"{cls.__name__} is a real-sample word (iq_mode=False); there is no I/Q slot order "
                f"to apply. Specialize with iq_mode=True to interleave.")
        i = np.asarray(getattr(i_stored, "val", i_stored), dtype=np.int64).ravel()
        q = np.asarray(getattr(q_stored, "val", q_stored), dtype=np.int64).ravel()
        if i.size != q.size:
            raise ValueError(f"I and Q must be the same length, got {i.size} and {q.size}.")
        lo, hi = (i, q) if cls.iq_order == "i_low" else (q, i)
        out = np.empty(i.size * 2, dtype=np.int64)
        out[0::2], out[1::2] = lo, hi
        return out

    @classmethod
    def iq_deinterleave(cls, slots: Any) -> tuple[np.ndarray, np.ndarray]:
        """The inverse of :meth:`iq_interleave`: one slot sequence -> ``(I, Q)``."""
        if not cls.iq_mode:
            raise ValueError(
                f"{cls.__name__} is a real-sample word (iq_mode=False); there is no I/Q slot order "
                f"to undo.")
        s = np.asarray(getattr(slots, "val", slots), dtype=np.int64).ravel()
        if s.size % 2:
            raise ValueError(f"an interleaved slot sequence has an even length, got {s.size}.")
        lo, hi = s[0::2], s[1::2]
        return (lo, hi) if cls.iq_order == "i_low" else (hi, lo)

    # -- reporting ---------------------------------------------------------------------------

    @classmethod
    def describe(cls) -> str:
        """One line naming every rule this word fixes — for error messages and docs."""
        kind = "complex" if cls.iq_mode else "real"
        layout = (f"{cls.bits_per_samp}-in-{cls.bits_per_samp_pack} ({cls.justify}-justified)"
                  if cls.bits_per_samp != cls.bits_per_samp_pack else f"{cls.bits_per_samp}-bit")
        order = f", {cls.iq_order}" if cls.iq_mode else ""
        return (f"{cls.samp_per_word} {kind} sample(s)/beat, {layout}{order} "
                f"-> {cls.bitwidth}-bit word")


class Rfsoc4x2SampWord(RfdcSampWord):
    """The **RFSoC 4x2** (Zynq UltraScale+ ZU48DR) converter word.

    A preset is an ordinary subclass restating only what the board fixes, so
    ``Rfsoc4x2SampWord.specialize(samp_per_word=4)`` keeps the board's sample geometry and asks only
    for the beat width the design wants.

    The ZU48DR's RF-ADCs and RF-DACs resolve **14** bits; the AXI-Stream carries each sample in a
    **16**-bit slot.  Those are two numbers, and this is where they stop being one.

    Until 2026-08-21 this preset said 16 in a 16-bit slot, because that is what every RF example
    declared as ``nbits`` — a number chosen to match the *bus*, which silently made the model's
    quantization four times finer than the part's.  Correcting it is a deliberate change to what the
    model reports, not a refactor: quantization noise is the one effect this converter model exists
    to reproduce bit-exactly, so understating it was the whole defect.

    :attr:`~RfdcSampWord.justify` is inherited, **and its default is unconfirmed** — see the field.
    On a part where the two widths are equal it did not matter; here it decides where four of every
    sixteen bit patterns land, so this preset is the first place its value has an observable
    consequence.
    """

    bits_per_samp: ClassVar[int] = 14
    bits_per_samp_pack: ClassVar[int] = 16


# ------------------------------------------------------------------------------------------------
# The public conversion pair
# ------------------------------------------------------------------------------------------------
#
# Before these, turning samples into words meant composing three calls with two easily-swapped type
# arguments::
#
#     words = write_array(W.to_slots(from_real(x, W.samp_type())),
#                         elem_type=W.slot_type(), word_bw=W.bitwidth)
#
# That recipe is correct — and it is exactly the ``get_pipelined`` / ``write_pipelined`` failure
# mode: correct usage that is silent-if-wrong and undiscoverable.  ``samp_type()`` where
# ``slot_type()`` belongs is the effective-vs-container confusion this module exists to prevent, and
# it lays the samples out at the wrong stride — a different word, no exception.  So the composition
# gets a name, and the two type arguments stop being the caller's to get right.
#
# ``write_array`` / ``read_array`` are imported inside the functions rather than at module scope:
# ``arrayutils`` pulls in ``waveflow.build``, and a schema type should not drag the build stack into
# every import of itself.  Same reason ``rf_samp_buf.pack_samples`` does it.


def _as_word_type(word_type: Any, fn: str) -> type[RfdcSampWord]:
    """The one argument both functions share, checked once and with the fix in the message."""
    if not (isinstance(word_type, type) and issubclass(word_type, RfdcSampWord)):
        raise TypeError(
            f"{fn}() takes the WORD TYPE as its first argument — the packing convention, not a "
            f"width. Got {word_type!r}. Build one with RfdcSampWord.specialize(...) or a board "
            f"preset such as Rfsoc4x2SampWord.specialize(samp_per_word=4).")
    return word_type


def _chunks_per_word(word_type: type[RfdcSampWord]) -> int:
    """``uint64`` chunks one word occupies — ``1`` up to 64 bits, ``ceil(bitwidth / 64)`` above."""
    return -(-int(word_type.bitwidth) // 64)


def _word_row_shape(word_type: type[RfdcSampWord], n_words: int) -> tuple[int, ...]:
    """The shape one channel's packed words occupy: ``(n_words,)``, or ``(n_words, k)`` over 64 bits.

    The wide case is not something invented here — it is the ``(n, k)`` little-endian ``uint64``
    convention the serializers already use and the rest of Waveflow already reads.  It is spelled out
    in one place so the two functions cannot decide it differently.
    """
    if int(word_type.bitwidth) <= 64:
        return (int(n_words),)
    return (int(n_words), _chunks_per_word(word_type))


def pack(word_type: type[RfdcSampWord], samps: Any) -> np.ndarray:
    """Stored sample **integers** ``(n_ch, n_samp)`` -> packed AXIS words ``(n_ch, n_words)``.

    The inverse of :func:`unpack`, exactly: nothing is padded, truncated or rounded here, so
    ``unpack(W, pack(W, x))`` returns ``x``.

    Parameters
    ----------
    word_type
        The :class:`RfdcSampWord` subclass this stream is packed to.  **Explicit on both sides of
        the pair** — see :func:`unpack`, where it cannot be inferred, and so is not inferred here
        either.
    samps
        A 2-D ``(n_ch, n_samp)`` array of **stored integers**, channel-major, or a ``DataArray`` over
        them.  Complex when ``word_type.iq_mode``, with integral real and imaginary parts.

    Integers, not fixed-point
    -------------------------
    A real-valued input would make this function **lossy** — quantization happening inside a call
    whose name says formatting — and the one place quantization must not hide is a function the
    caller believes is bit-shuffling.  The two questions stay in two calls::

        stored = from_real(x, Word.samp_type())   # quantize — the CONVERTER's question,
                                                  #            at bits_per_samp
        words  = pack(Word, stored)               # lay out   — the BUS's question, lossless

    So a float array is refused rather than quantized, and the message names the missing call.  The
    caller therefore knows the amplitude scale, which is right: ``full_scale`` is a property of the
    converter (:class:`~examples.rf_loopback.rfdc.Rfdc`), not of the word.

    Channel-major, per channel
    --------------------------
    ``(n_ch, n_samp)`` because the RF side is already ``(n_ch, blksize)``; the other order puts a
    transpose at every boundary crossing.  Each channel is packed **independently** into its own row
    of words, which deliberately declines to answer the open "one AXIS port per channel, or one wide
    interleaved port?" question in ``plans/adc_model.md``: interleaving these rows afterwards is a
    separate step, while de-interleaving a committed layout is not.

    Raises
    ------
    ValueError
        If ``n_samp`` is not a whole number of words.  **Never padded** — that refusal is what makes
        ``n_samp = n_words * samp_per_word`` exact on the way back, so :func:`unpack` needs no length
        argument.  Also if a value does not fit ``bits_per_samp``: an over-range sample would shift
        into its neighbour's slot and corrupt it silently.
    TypeError
        If the samples are not stored integers, or their realness disagrees with ``iq_mode``.
    """
    from waveflow.hw.arrayutils import write_array

    W = _as_word_type(word_type, "pack")
    arr = np.asarray(getattr(samps, "val", samps))

    if arr.ndim != 2:
        raise ValueError(
            f"pack() takes a 2-D (n_ch, n_samp) channel-major array, got shape {arr.shape}. One "
            f"channel is (1, n_samp) — reshape(1, -1) — not (n_samp,): the shape is part of the "
            f"contract, so that pack and unpack are inverses in shape as well as in value.")
    n_ch, n_samp = int(arr.shape[0]), int(arr.shape[1])

    spw = int(W.samp_per_word)
    if n_samp % spw:
        raise ValueError(
            f"pack(): {n_samp} samples is not a whole number of {spw}-sample words for "
            f"{W.describe()}. A sample cannot straddle a beat, and this pads nothing — the refusal "
            f"is what makes the sample count exact on the way back through unpack().")
    n_words = n_samp // spw

    parts: tuple[np.ndarray, ...]
    if np.iscomplexobj(arr):
        if not W.iq_mode:
            raise TypeError(
                f"pack(): {W.__name__} is a real-sample word (iq_mode=False) but the samples are "
                f"complex. Specialize with iq_mode=True, or carry I and Q as two real channels.")
        parts = (arr.real, arr.imag)
        for part in parts:
            if part.size and not np.all(part == np.rint(part)):
                raise TypeError(
                    f"pack() takes STORED INTEGERS; these complex samples have non-integral parts. "
                    f"Quantize first — from_real(x, {W.__name__}.samp_type()) on each of I and Q — "
                    f"then pack. Quantizing here would make a formatting call lossy.")
    else:
        if W.iq_mode:
            raise TypeError(
                f"pack(): {W.__name__} carries interleaved I/Q (iq_mode=True), so it takes a "
                f"COMPLEX sample array; got dtype {arr.dtype}. Pass i + 1j * q, in stored integers.")
        if not np.issubdtype(arr.dtype, np.integer):
            raise TypeError(
                f"pack() takes STORED INTEGERS, got dtype {arr.dtype}. Quantizing here would make a "
                f"formatting call lossy, so it is a separate step: "
                f"stored = from_real(x, {W.__name__}.samp_type()), then pack({W.__name__}, stored).")
        parts = (arr,)

    lo, hi = -(1 << (int(W.bits_per_samp) - 1)), (1 << (int(W.bits_per_samp) - 1)) - 1
    for part in parts:
        if part.size and (part.min() < lo or part.max() > hi):
            raise ValueError(
                f"pack(): a stored sample is outside the {int(W.bits_per_samp)}-bit range "
                f"[{lo}, {hi}] that {W.__name__} resolves (saw [{part.min():g}, {part.max():g}]). "
                f"An over-range value shifts into the next slot and corrupts it silently. "
                f"from_real() saturates into range; a hand-built array has to as well.")

    out = np.zeros((n_ch,) + _word_row_shape(W, n_words), dtype=np.uint64)
    slot_type, word_bw = W.slot_type(), int(W.bitwidth)
    for ch in range(n_ch):
        if not n_words:
            continue
        if W.iq_mode:
            slots = W.iq_interleave(np.rint(arr[ch].real).astype(np.int64),
                                    np.rint(arr[ch].imag).astype(np.int64))
        else:
            slots = np.asarray(arr[ch], dtype=np.int64).ravel()
        # to_slots is the justification rule — the ONE thing a serializer cannot know.  The
        # word<->slot step is the serializer's, never a .range() written here.
        words = np.asarray(write_array(W.to_slots(slots), elem_type=slot_type, word_bw=word_bw))
        if words.shape[0] != n_words:  # pragma: no cover — the geometry makes this unreachable
            raise AssertionError(
                f"pack(): {W.describe()} packed {n_samp} samples into {words.shape[0]} words, "
                f"expected {n_words}.")
        out[ch] = words.reshape(_word_row_shape(W, n_words))
    return out


def unpack(word_type: type[RfdcSampWord], samp_words: Any) -> np.ndarray:
    """Packed AXIS words ``(n_ch, n_words)`` -> stored sample integers ``(n_ch, n_samp)``.

    The exact inverse of :func:`pack`, and it needs no length argument: ``pack`` refuses a sample
    count that is not a whole number of words, so ``n_samp = n_words * samp_per_word`` always.

    Returns ``int64`` samples, or ``complex128`` ones with integral parts when
    ``word_type.iq_mode``.  Turn them back into amplitudes the way they were quantized —
    ``to_real(array(Word.samp_type(), stored))`` — which is the two-questions-two-calls split
    :func:`pack` describes, run backwards.

    Why the type is an argument
    ---------------------------
    ``unpack(samp_words)`` cannot work.  Packed words are a bare ``uint64`` array and a stream
    ``get()`` hands you exactly that — no container, no ``element_type``, nothing a convention could
    be recovered from.  Returning a ``DataArray[Word]`` from :func:`pack` would let this infer the
    type for arrays that came from :func:`pack`, and never for the ones that came off a wire, which
    is the case that matters.  Symmetry beats the shorter signature.
    """
    from waveflow.hw.arrayutils import read_array

    W = _as_word_type(word_type, "unpack")
    arr = np.asarray(getattr(samp_words, "val", samp_words))

    want_ndim = 2 if int(W.bitwidth) <= 64 else 3
    if arr.ndim != want_ndim:
        wide = "" if want_ndim == 2 else (
            f", the trailing axis being the {_chunks_per_word(W)} little-endian uint64 chunks a "
            f"{int(W.bitwidth)}-bit word occupies")
        raise ValueError(
            f"unpack() takes the {want_ndim}-D (n_ch, n_words"
            f"{'' if want_ndim == 2 else ', k'}) array pack() returns{wide}; got shape "
            f"{arr.shape} for {W.describe()}. One channel is (1, n_words) — reshape(1, -1) — not "
            f"(n_words,).")
    if want_ndim == 3 and int(arr.shape[2]) != _chunks_per_word(W):
        raise ValueError(
            f"unpack(): {W.describe()} occupies {_chunks_per_word(W)} uint64 chunks per word, but "
            f"the array carries {int(arr.shape[2])}.")

    n_ch, n_words = int(arr.shape[0]), int(arr.shape[1])
    n_samp = n_words * int(W.samp_per_word)
    out = np.zeros((n_ch, n_samp), dtype=np.complex128 if W.iq_mode else np.int64)
    slot_type, word_bw = W.slot_type(), int(W.bitwidth)
    for ch in range(n_ch):
        if not n_words:
            continue
        slots = read_array(arr[ch], elem_type=slot_type, word_bw=word_bw,
                           shape=n_words * W.slots_per_word())
        stored = W.from_slots(slots)
        if W.iq_mode:
            i, q = W.iq_deinterleave(stored)
            out[ch] = i + 1j * q
        else:
            out[ch] = stored
    return out
