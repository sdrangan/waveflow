"""rf_relayout.py — the buffer's **logic-side re-layout**: converter slots <-> densely-packed samples.

``plans/rf_shot_buf.md`` § *The logic-side port*, and the Stage A gate that goes with it.

**The buffer exposes samples; it owns the converter's packing.**  A user's logic — and a host loading
a waveform into an ``m_axi`` arena — should not have to know about ``justify``, ``iq_order`` or
14-in-16.  ``plans/adc_model.md`` § *The logic-side interface* weighed three candidates and chose
option 2: ``ap_uint<W>`` of **densely-packed effective-width samples**.  Option 1 (the
:class:`~waveflow.hw.rfdc_samp_word.RfdcSampWord` itself) would make every user write the
justification; option 3 (one ``ap_int<bits_per_samp>`` per beat) caps throughput at ``f_axis``, which
is the entire reason packing exists.

So there is a conversion, and this module is it::

    RFDC word:   [ slot3 | slot2 | slot1 | slot0 ]   4 x 16 bits, 14 effective, left-justified
                    |       |       |       |        >> 2 each
    dense word:  [0000000][ s3  ][ s2  ][ s1  ][ s0 ]  4 x 14 bits, 8 idle high bits

**Nothing here hand-rolls a ``.range()``.**  The word<->slot step is the generated serializer's
(``rf_slot_elem_array_utils.h`` / ``rf_dense_elem_array_utils.h``); the only arithmetic this module
owns is the **justification shift**, which is the one rule a serializer cannot know.  That split is
the same one :func:`waveflow.hw.rfdc_samp_word.pack` makes, and it is why the C++ and the Python
twin cannot disagree about slot order — neither of them decides it.

Why this is measured rather than predicted
------------------------------------------
**When ``bits_per_samp == bits_per_samp_pack`` the re-layout is the identity**, which is every
configuration in this repo except the RFSoC 4x2 preset.  The path is therefore unexercised, and
"shift and mask per slot holds II=1" was a *prediction*.  ``plans/adc_model.md`` is explicit that it
must be gated on a csynth before anything is designed around it, and names why: the loader-hoist
reversal (commit ``a2f93e0``), where csynth reported II=1 and **the RTL played 0xFFFF for 9984
samples while every counter reported success.**  ``examples/rf_relayout`` is that gate — a csynth for
the II and an XSI run for the bits.

Round trips, and which one is exact
-----------------------------------
``to_slots(to_dense(x)) == x`` **only when each slot's low ``justify_shift()`` bits are zero**, which
is exactly what a left-justified converter guarantees and what a hand-built test vector must
therefore respect (``examples/rf_samp_buf_rx`` calls the same quantity ``SAMP_STEP``).  The other
order — ``to_dense(to_slots(d)) == d`` — is exact unconditionally, because nothing is discarded
going out to the wider slot.  A gate that used a step-by-one ramp would be measuring the
justification rather than the design.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, ClassVar

import numpy as np

from waveflow.hw.clock import Clock
from waveflow.hw.codegen_targets import COMPOSITE_KERNEL
from waveflow.hw.dataschema import IntField
from waveflow.hw.hw_freerun import FreeRunMod
from waveflow.hw.hw_module import HwParam
from waveflow.hw.interface import StreamIF, StreamIFMaster, StreamIFSlave
from waveflow.hw.mem_stream import KernelTask
from waveflow.simulation.simobj import ProcessGen

#: Where the generated array-utils headers land, and therefore where the task bodies find them by
#: plain name.  The task headers are copied into the same directory, so ``#include
#: "rf_slot_elem_array_utils.h"`` resolves without a path.
INCLUDE_DIR = "include"

_ELEM_CACHE: dict[tuple[str, int, str], type[IntField]] = {}


def _named_elem(class_name: str, bits: int, include_dir: str) -> type[IntField]:
    """An :class:`~waveflow.hw.dataschema.IntField` of *bits* bits carrying a **fixed class name**.

    The name is what the generated header and namespace are derived from
    (``_array_utils_stem`` reads ``__name__``), so a fixed name is what lets a *framework* task body
    write ``#include "rf_slot_elem_array_utils.h"`` without knowing the width.  The width still
    varies per build — each example builds in its own tree and gets its own copy of the header.

    A distinct **subclass** rather than renaming a specialization in place, for the reason
    ``examples/interleaver``'s ``IlElem`` spells out: ``specialize`` returns a *cached* class keyed by
    ``(bitwidth, signed, include_dir)``, so renaming it would corrupt every other example that
    specialized the same key.
    """
    key = (class_name, int(bits), str(include_dir))
    cached = _ELEM_CACHE.get(key)
    if cached is not None:
        return cached
    base = IntField.specialize(bitwidth=int(bits), signed=True, include_dir=str(include_dir))
    cls = type(class_name, (base,), {"__module__": __name__})
    _ELEM_CACHE[key] = cls
    return cls


def slot_elem_type(word, include_dir: str = INCLUDE_DIR) -> type[IntField]:
    """The **container** element — one converter slot, signed.  ``rf_slot_elem_array_utils.h``.

    Signed because a justified sample read back as a plain integer must sign-extend the way the RTL's
    ``ap_int`` does.  It is :meth:`~waveflow.hw.rfdc_samp_word.RfdcSampWord.slot_type`'s width under
    a fixed name — the width comes from the word type, the name from here.
    """
    return _named_elem("RfSlotElem", int(word.bits_per_samp_pack), include_dir)


def dense_elem_type(word, include_dir: str = INCLUDE_DIR) -> type[IntField]:
    """The **effective** element — one sample at its converter resolution.  ``rf_dense_elem_array_utils.h``."""
    return _named_elem("RfDenseElem", int(word.bits_per_samp), include_dir)


def slots_per_word(word) -> int:
    """Slots one word carries — ``samp_per_word``, doubled for interleaved I/Q.

    Read off the word type rather than restated, so I/Q is not a second place this module could
    disagree with the converter about how many things are in a beat.
    """
    return int(word.slots_per_word())


def check_geometry(word) -> None:
    """Refuse a word the dense layout cannot carry at the same width — **before** anything is built.

    The dense side must fit in the *same* word as the converter side, because that is what makes this
    a re-layout inside one width rather than a width conversion (``plans/adc_model.md`` § *Take 64
    bits, not 56*).  It holds for every real part — dense is by definition narrower — so this is a
    guard against a mis-specialized word type, not a design limitation.
    """
    n = slots_per_word(word)
    need = n * int(word.bits_per_samp)
    if need > int(word.bitwidth):
        raise ValueError(
            f"{word.__name__}: {n} densely-packed {int(word.bits_per_samp)}-bit samples need "
            f"{need} bits but the word is {int(word.bitwidth)}. The dense port is deliberately the "
            f"SAME width as the converter's word so the conversion is a pure re-layout; a word type "
            f"where that fails is mis-specialized.")


# ---------------------------------------------------------------------------
# The Python twins — the pysim golden, and the only place the shift lives on this side
# ---------------------------------------------------------------------------

def to_dense(word, samp_words: Any) -> np.ndarray:
    """Converter words -> **densely-packed** words of the same width.

    Two calls and one shift, in that order: the serializer's ``read_array`` splits the word into
    slots, :meth:`~waveflow.hw.rfdc_samp_word.RfdcSampWord.from_slots` un-justifies them (an
    *arithmetic* shift, so a negative sample survives), and the serializer's ``write_array`` lays
    them back down at the dense stride.  The C++ body does exactly these three things in exactly this
    order, which is what makes the two backends bit-identical by construction rather than by test.
    """
    from waveflow.hw.arrayutils import read_array, write_array

    check_geometry(word)
    n = slots_per_word(word)
    raw = np.asarray(getattr(samp_words, "val", samp_words), dtype=np.uint64).ravel()
    slots = read_array(raw, elem_type=word.slot_type(), word_bw=int(word.bitwidth),
                       shape=int(raw.size) * n)
    stored = word.from_slots(np.asarray(getattr(slots, "val", slots), dtype=np.int64).ravel())
    dense = write_array(stored, elem_type=dense_elem_type(word), word_bw=int(word.bitwidth))
    return np.asarray(dense, dtype=np.uint64).ravel()


def to_slots(word, dense_words: Any) -> np.ndarray:
    """The inverse: densely-packed words -> converter words of the same width.

    Exact in this direction unconditionally — nothing is discarded going out to the wider slot.
    """
    from waveflow.hw.arrayutils import read_array, write_array

    check_geometry(word)
    n = slots_per_word(word)
    raw = np.asarray(getattr(dense_words, "val", dense_words), dtype=np.uint64).ravel()
    stored = read_array(raw, elem_type=dense_elem_type(word), word_bw=int(word.bitwidth),
                        shape=int(raw.size) * n)
    slots = word.to_slots(np.asarray(getattr(stored, "val", stored), dtype=np.int64).ravel())
    out = write_array(slots, elem_type=word.slot_type(), word_bw=int(word.bitwidth))
    return np.asarray(out, dtype=np.uint64).ravel()


# ---------------------------------------------------------------------------
# The two tasks
# ---------------------------------------------------------------------------

@dataclass
class _RelayoutTask(FreeRunMod):
    """What the two directions share: one word in, one word out, per firing, at the same width."""

    #: Word width in bits — **both** ports.  The whole point of the 64-bit choice is that the two
    #: sides are the same width, so this is one number rather than two.
    bitwidth: HwParam[int] = 64
    #: Slots (or dense samples) in one word.
    n_slot: HwParam[int] = 4
    #: Bits the effective sample is shifted left by inside its container slot —
    #: :meth:`~waveflow.hw.rfdc_samp_word.RfdcSampWord.justify_shift`.  **0 makes this the
    #: identity**, which is every configuration in the repo but the 4x2 preset and is exactly why the
    #: path had to be gated on a build that is not.
    shift: HwParam[int] = 2
    clk: Clock = field(default_factory=lambda: Clock(freq=250e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        w = int(self.bitwidth)
        self.s_in = StreamIFSlave(sim=self.sim, name=f"{self.name}_s_in", bitwidth=w, has_tlast=True)
        self.s_out = StreamIFMaster(sim=self.sim, name=f"{self.name}_s_out", bitwidth=w,
                                    has_tlast=True)
        for ep in (self.s_in, self.s_out):
            self.add_endpoint(ep)

    def _template_args(self) -> tuple[int, ...]:
        return (int(self.bitwidth), int(self.n_slot), int(self.shift))

    def _convert(self, words: np.ndarray) -> np.ndarray:  # pragma: no cover - overridden
        raise NotImplementedError

    def run_iter(self) -> ProcessGen[None]:
        """One word per firing — the pysim twin of a ``while (1) { … }`` body at II=1.

        One word per burst in the scenario, for ``bram_access``'s reason: a pysim slave dequeues a whole
        burst per ``get`` and ``nwords_max`` *discards* the remainder, so a multi-word burst would be
        one pysim firing against many RTL firings.
        """
        words = yield from self.s_in.get(nwords_max=1)
        out = self._convert(np.asarray(words, dtype=np.uint64).ravel()[:1])
        yield from self.s_out.write(np.asarray(out, dtype=np.uint64))


@dataclass
class RfRelayoutToDense(_RelayoutTask):
    """Converter word in, **densely-packed** word out — the direction an RX path takes.

    The hardware body is ``waveflow/build/rf_relayout_to_dense_task.h``; this class's
    :meth:`_convert` is the pysim twin, and both go through the *same* generated serializers.
    """

    cpp_kernel_name: ClassVar[str | None] = "rf_relayout_to_dense"

    def kernel_task(self) -> KernelTask:
        return KernelTask("rf_relayout_to_dense_task", "rf_relayout_to_dense_task.h",
                          ("s_in", "s_out"), template_args=self._template_args())

    def _convert(self, words: np.ndarray) -> np.ndarray:
        return _twin(self, words, dense=True)


@dataclass
class RfRelayoutToSlots(_RelayoutTask):
    """Densely-packed word in, **converter** word out — the direction a TX path takes."""

    cpp_kernel_name: ClassVar[str | None] = "rf_relayout_to_slots"

    def kernel_task(self) -> KernelTask:
        return KernelTask("rf_relayout_to_slots_task", "rf_relayout_to_slots_task.h",
                          ("s_in", "s_out"), template_args=self._template_args())

    def _convert(self, words: np.ndarray) -> np.ndarray:
        return _twin(self, words, dense=False)


def _twin(task: _RelayoutTask, words: np.ndarray, *, dense: bool) -> np.ndarray:
    """Run one word through the module-level twin, on a word type rebuilt from the task's integers.

    The task carries three integers rather than a word type (a type cannot be an ``HwParam``), so the
    twin is reached by reconstructing the word those integers describe.  Reconstructing rather than
    reimplementing is the point: :func:`to_dense` / :func:`to_slots` stay the single Python-side
    statement of the conversion.
    """
    from waveflow.hw.rfdc_samp_word import RfdcSampWord

    n, shift, w = int(task.n_slot), int(task.shift), int(task.bitwidth)
    cont = w // n
    word = RfdcSampWord.specialize(samp_per_word=n, bits_per_samp=cont - shift,
                                   bits_per_samp_pack=cont,
                                   justify="left" if shift else "right")
    return to_dense(word, words) if dense else to_slots(word, words)


# ---------------------------------------------------------------------------
# The composite: both directions, back to back
# ---------------------------------------------------------------------------

@dataclass
class RfRelayout(FreeRunMod):
    """Converter word -> dense -> converter word: the re-layout **and its inverse**, in one kernel.

    A loopback rather than a single direction, for three reasons that are all about what a gate can
    prove:

    * **Both IIs come out of one csynth.**  The two directions are separate synthesized modules with
      their own reports, so one build measures the number Stage A exists to measure — twice.
    * **The identity is checkable without a golden table.**  For any input whose slots are properly
      justified, the output equals the input, so an RTL run can be graded against the *stimulus*
      rather than against a second implementation of the conversion.
    * **A shift in the wrong direction cannot cancel.**  ``>>`` then ``<<`` by the same amount is the
      identity, but only because the two tasks read the shift from the same parameter; a body that
      re-derived it would show up as a value that no longer round-trips, and the low-bit test below
      is what makes that visible rather than absorbed.

    **What it does not prove**: that the dense layout in the middle is the one a *host* would write.
    Only a byte-level check of the intermediate words does that, and
    ``tests/examples/test_rf_relayout.py`` makes it against :func:`to_dense`.
    """

    cpp_kernel_name: ClassVar[str | None] = "rf_relayout"
    potential_targets: ClassVar[frozenset[str]] = frozenset({COMPOSITE_KERNEL})

    bitwidth: HwParam[int] = 64
    n_slot: HwParam[int] = 4
    shift: HwParam[int] = 2
    clk: Clock = field(default_factory=lambda: Clock(freq=250e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        w, n, s = int(self.bitwidth), int(self.n_slot), int(self.shift)
        self.to_dense = RfRelayoutToDense(sim=self.sim, name=f"{self.name}_to_dense", bitwidth=w,
                                          n_slot=n, shift=s, clk=self.clk)
        self.to_slots = RfRelayoutToSlots(sim=self.sim, name=f"{self.name}_to_slots", bitwidth=w,
                                          n_slot=n, shift=s, clk=self.clk)
        self.add_comp(self.to_dense)
        self.add_comp(self.to_slots)

        mid = StreamIF(name=f"{self.name}_dense_if", sim=self.sim, clk=self.clk, bitwidth=w)
        mid.bind(ep_name="master", endpoint=self.to_dense.s_out)
        mid.bind(ep_name="slave", endpoint=self.to_slots.s_in)
        self.add_if(mid)

        #: ``add_comp`` x ``add_endpoint`` order with the internal channel's endpoints removed.
        self.boundary = ["s_in", "s_out"]
        self.s_in = self.to_dense.s_in
        self.s_out = self.to_slots.s_out

    @classmethod
    def for_word(cls, word, **kwargs) -> "RfRelayout":
        """Build the pair from a converter word type — the single place the three integers are derived."""
        check_geometry(word)
        return cls(bitwidth=int(word.bitwidth), n_slot=slots_per_word(word),
                   shift=int(word.justify_shift()), **kwargs)

    @property
    def is_identity(self) -> bool:
        """``True`` when this configuration's re-layout does nothing.

        The caveat ``plans/rf_shot_buf.md`` § *The caveat, and it is a Stage A gate* is about: a
        build with ``shift == 0`` **and** equal strides measures the identity, and a measurement of
        the identity is not a measurement of the re-layout.  A gate should assert this is ``False``.
        """
        return int(self.shift) == 0
