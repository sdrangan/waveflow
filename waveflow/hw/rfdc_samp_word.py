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
        pack = int(cls.bits_per_samp_pack if bits_per_samp_pack is None else bits_per_samp_pack)
        iq = bool(cls.iq_mode if iq_mode is None else iq_mode)
        just = str(cls.justify if justify is None else justify)
        order = str(cls.iq_order if iq_order is None else iq_order)

        if spw < 1:
            raise ValueError(f"samp_per_word must be at least 1, got {spw}.")
        if eff < 2:
            raise ValueError(
                f"bits_per_samp must be at least 2 (a signed sample needs a sign bit and a "
                f"magnitude bit), got {eff}.")
        if pack < eff:
            raise ValueError(
                f"bits_per_samp_pack={pack} is narrower than bits_per_samp={eff}: the container "
                f"cannot be smaller than what it contains. bits_per_samp is what the converter "
                f"RESOLVES; bits_per_samp_pack is the slot it rides in.")
        if just not in JUSTIFY:
            raise ValueError(f"justify must be one of {JUSTIFY}, got {just!r}.")
        if order not in IQ_ORDER:
            raise ValueError(f"iq_order must be one of {IQ_ORDER}, got {order!r}.")

        overrides = cls.validate_specialize_kwargs(kwargs)
        override_items = tuple(sorted(overrides.items()))
        key = (cls, spw, eff, pack, iq, just, order, override_items)
        cached = cls._specializations.get(key)
        if cached is not None:
            return cached

        bw = spw * pack * (2 if iq else 1)
        subclass_name = (f"RfdcSampWord{spw}x{eff}" + (f"in{pack}" if pack != eff else "")
                         + ("_iq" if iq else ""))
        attrs = cls.merge_specialize_attrs(
            {
                "samp_per_word": spw, "bits_per_samp": eff, "bits_per_samp_pack": pack,
                "iq_mode": iq, "justify": just, "iq_order": order,
                "bitwidth": bw, "signed": False, "cpp_type": f"ap_uint<{bw}>",
                "__module__": cls.__module__,
                "__doc__": (
                    f"{spw} {'complex' if iq else 'real'} sample(s) per beat, {eff} effective bits "
                    f"in a {pack}-bit slot, {just}-justified: a {bw}-bit word."),
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
        pack = (f"{cls.bits_per_samp}-in-{cls.bits_per_samp_pack} ({cls.justify}-justified)"
                if cls.bits_per_samp != cls.bits_per_samp_pack else f"{cls.bits_per_samp}-bit")
        order = f", {cls.iq_order}" if cls.iq_mode else ""
        return (f"{cls.samp_per_word} {kind} sample(s)/beat, {pack}{order} "
                f"-> {cls.bitwidth}-bit word")


class Rfsoc4x2SampWord(RfdcSampWord):
    """The **RFSoC 4x2** (Zynq UltraScale+ ZU48DR) converter word.

    A preset is an ordinary subclass restating only what the board fixes, so
    ``Rfsoc4x2SampWord.specialize(samp_per_word=4)`` keeps the board's sample geometry and asks only
    for the beat width the design wants.

    The ZU48DR's RF-ADCs and RF-DACs resolve **14** bits; the AXI-Stream carries each sample in a
    **16**-bit slot.  Those are two numbers, and this is where they stop being one.

    **Not yet:** this commit introduces the type with *today's* numbers everywhere, so that the
    refactor is provably behaviour-preserving — 16 effective bits in a 16-bit slot, which is what
    every RF example has been declaring as ``nbits = 16``.  Setting it to 14-in-16 changes
    quantization on purpose and is the next commit's job, where the numbers that move are accounted
    for one at a time.

    :attr:`~RfdcSampWord.justify` is inherited, **and its default is unconfirmed** — see the field.
    """

    bits_per_samp: ClassVar[int] = 16
    bits_per_samp_pack: ClassVar[int] = 16
