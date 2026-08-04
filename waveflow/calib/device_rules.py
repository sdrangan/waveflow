"""device_rules.py — the device physics a resource prior stands on, in one place.

A resource *prior* is a formula rather than a fit, and its content splits cleanly in two:

* **What the design has** — how many multipliers, at what operand width; how many memory banks, how
  deep, of what element.  Only the design's author knows these, and they are read off the elaborated
  module.
* **What the device costs them** — that a DSP48E1 multiplier is 25x18, that two narrow multiplies
  pack into one, that a BRAM18 has legal port shapes rather than a bag of bits.  Nobody's design
  changes any of that.

This module owns the second half, so a model author supplies only the first.  Before it existed the
DSP48E1 geometry lived in ``examples/fir_block/fir_block_resource.py`` and the BRAM18 geometry in
``examples/vecmult/vecmult_corpus.py`` -- two private copies of the same part, with nothing keeping
them honest and a third copy accruing in prose on a docs page.

Rules are keyed on the **part**, because that is what they are properties of: a DSP48E1 is 25x18 and
a DSP48E2 is 27x18, and a model written against one is simply wrong on the other.  That is the same
reasoning that puts ``res_types`` on :class:`~waveflow.calib.platform.Platform` rather than in a
global.

WHAT THIS MODULE WILL NOT DO.  It reports what the device *can* hold, not what HLS *chose*.  Binding
is a tool decision, and where the tool's choice is not determined by geometry -- the band where a
partitioned array may go to LUTRAM instead of block RAM -- the rule says ``uncertain`` rather than
guessing.  A prior that quietly picks a side there would be a fit with one sample, wearing a
formula's authority.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Device geometry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DspGeometry:
    """The multiplier ports of one hard DSP primitive."""

    name: str
    #: The wider operand port, in bits (25 on a DSP48E1).
    port_a_bits: int
    #: The narrower operand port, in bits (18 on a DSP48E1).
    port_b_bits: int
    #: At or below this operand width two multiplies share one DSP -- a packing *win*.
    pack_bits: int


@dataclass(frozen=True)
class BramGeometry:
    """The shape of one hard memory block."""

    name: str
    #: Usable bits in one block (18432 for a BRAM18).
    total_bits: int
    #: Legal port widths, ascending.  A block is not a bag of bits: an array of 16-bit elements uses
    #: the 18-bit shape and gets 1024 entries, not ``18432/16 = 1152``.
    port_widths: tuple
    #: Largest **bits per bank** observed to land in distributed RAM rather than a block.
    lutram_max_bank_bits: int
    #: Smallest bits per bank observed to land in a block.  Between the two the rule reports
    #: ``uncertain`` rather than picking a side -- see :func:`bram_estimate`.
    bram_min_bank_bits: int


@dataclass(frozen=True)
class Device:
    """The primitives one part family is built from."""

    family: str
    dsp: DspGeometry
    bram: BramGeometry


#: 7-series / Zynq-7000: DSP48E1 (25x18) and the 18 Kb block RAM.
SERIES7 = Device(
    family="series7",
    dsp=DspGeometry(name="DSP48E1", port_a_bits=25, port_b_bits=18, pack_bits=8),
    bram=BramGeometry(name="BRAM18", total_bits=18432, port_widths=(1, 2, 4, 9, 18),
                      lutram_max_bank_bits=1008, bram_min_bank_bits=1024),
)

#: UltraScale / UltraScale+: DSP48E2 widens the A port to 27 bits.  The BRAM18 shape is unchanged.
ULTRASCALE = Device(
    family="ultrascale",
    dsp=DspGeometry(name="DSP48E2", port_a_bits=27, port_b_bits=18, pack_bits=8),
    bram=BramGeometry(name="BRAM18", total_bits=18432, port_widths=(1, 2, 4, 9, 18),
                      lutram_max_bank_bits=1008, bram_min_bank_bits=1024),
)

#: Part-prefix -> device.  Longest prefix wins, so a more specific entry can be added later without
#: disturbing the general one.
_PART_PREFIXES: dict = {
    "xc7":  SERIES7,      # Artix-7 / Kintex-7 / Virtex-7
    "xc7z": SERIES7,      # Zynq-7000
    "xczu": ULTRASCALE,   # Zynq UltraScale+
    "xcu":  ULTRASCALE,   # Virtex/Kintex UltraScale+
    "xcvu": ULTRASCALE,
    "xcku": ULTRASCALE,
}

#: Used when no part is supplied.  The reference platform in this tree is an xc7z020, and every
#: committed corpus was measured on one.
DEFAULT_DEVICE = SERIES7


class UnknownPartError(KeyError):
    """No device rule is registered for a part.

    Fatal rather than defaulted: silently applying 7-series geometry to an UltraScale+ part would
    produce a prior that looks exact and is wrong by a whole port width.
    """


def device_for(part: "str | None" = None, *, strict: bool = False) -> Device:
    """The :class:`Device` a part is built from.

    *part* may be a full part string (``xc7z020clg484-1``) or a family prefix.  ``None`` returns
    :data:`DEFAULT_DEVICE`.  With ``strict=True`` an unrecognized part raises instead of defaulting,
    which is what a published model should use.
    """
    if part is None:
        return DEFAULT_DEVICE
    p = str(part).lower()
    best = None
    for prefix, dev in _PART_PREFIXES.items():
        if p.startswith(prefix) and (best is None or len(prefix) > len(best[0])):
            best = (prefix, dev)
    if best is not None:
        return best[1]
    if strict:
        raise UnknownPartError(
            f"no device rule registered for part {part!r}; add it to _PART_PREFIXES rather than "
            f"letting a prior apply another family's geometry")
    return DEFAULT_DEVICE


class DeviceMismatchError(ValueError):
    """A model calibrated on one device is being asked to price another.

    Distinct from :class:`UnknownPartError`, which is "I have no rule for this part".  This is the
    subtler failure: there *is* a rule, it is simply a **different** one -- a DSP48E2 is 27x18 where
    a DSP48E1 is 25x18 -- and any fitted coefficients were measured against the wrong fabric.  Both
    halves of a model are invalidated, so it is refused rather than reported as low confidence.
    """


def require_same_device(part: "str | None", measured_on: "str | None", *, what: str = "model"):
    """Raise :class:`DeviceMismatchError` unless *part* and *measured_on* are the same device.

    The guard a calibrated model needs at install time.  Checking only that *part* is *known* is not
    enough -- that admits every other supported family and prices it with the wrong geometry, which
    looks exact and is wrong by a whole port width.
    """
    want, have = device_for(part, strict=True), device_for(measured_on, strict=True)
    if want is not have:
        raise DeviceMismatchError(
            f"{what} was calibrated on {measured_on!r} ({have.family}, {have.dsp.name}) but the "
            f"platform targets {part!r} ({want.family}, {want.dsp.name}); neither the device "
            f"geometry nor the fitted coefficients transfer between them")
    return want


# ---------------------------------------------------------------------------
# DSP
# ---------------------------------------------------------------------------


def dsp_per_mult(operand_bits: int, part: "str | None" = None) -> float:
    """DSPs consumed by one signed ``operand_bits x operand_bits`` multiply.

    Three regimes, from the port geometry alone:

    ==================================  =====================================================
    ``operand_bits <= pack_bits``        **0.5** -- two narrow multiplies share one DSP
    ``operand_bits <= port_b_bits``      **1**   -- fits the ports directly
    ``operand_bits <= port_a_bits``      **2**   -- one operand exceeds the narrow port, so split
    ==================================  =====================================================

    Beyond the wide port both operands need splitting and the cost grows as the product of the
    per-operand tile counts.  That last case is a documented extrapolation: no corpus in this tree
    measures it.
    """
    g = device_for(part).dsp
    w = int(operand_bits)
    if w <= g.pack_bits:
        return 0.5
    if w <= g.port_b_bits:
        return 1.0
    if w <= g.port_a_bits:
        return 2.0
    return math.ceil(w / g.port_b_bits) * math.ceil(w / g.port_a_bits)


def dsp_count(n_mult: int, operand_bits: int, part: "str | None" = None) -> int:
    """DSPs for *n_mult* multiplies of *operand_bits* operands.

    **What you supply:** how many multipliers your design instantiates, and how wide their operands
    are.  Both are structural facts about your body -- if the datapath has one multiplier per lane
    and ``LW`` lanes, ``n_mult = LW``.

    **What this supplies:** the device's port geometry.

    Rounds up once at the end, so a lone packed multiply still costs a whole DSP.
    """
    return int(math.ceil(int(n_mult) * dsp_per_mult(operand_bits, part)))


# ---------------------------------------------------------------------------
# BRAM
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BramEstimate:
    """What one partitioned array costs, and how much to believe it."""

    #: Block count.  Zero when *binding* is ``"lutram"``.
    blocks: int
    #: ``"bram"``, ``"lutram"``, or ``"uncertain"`` -- see :func:`bram_estimate`.
    binding: str
    #: Entries one block holds at this element width, after the port shape is chosen.
    entries_per_block: int
    #: Blocks placed in parallel to carry one element wider than a port.
    blocks_per_element: int

    @property
    def certain(self) -> bool:
        return self.binding != "uncertain"


def bram_shape(elem_bits: int, part: "str | None" = None) -> "tuple[int, int]":
    """``(blocks_per_element, entries_per_block)`` for an array of *elem_bits* elements.

    A block has legal port shapes, so the usable depth is ``total_bits / chosen_width`` and the
    chosen width is the smallest legal one that holds an element -- 16-bit elements use the 18-bit
    shape and get **1024** entries, not ``18432/16``.  Elements wider than the widest port are spread
    across blocks placed in parallel.
    """
    g = device_for(part).bram
    w = int(elem_bits)
    for pw in g.port_widths:
        if w <= pw:
            return 1, g.total_bits // pw
    widest = g.port_widths[-1]
    return math.ceil(w / widest), g.total_bits // widest


def bram_estimate(n_banks: int, depth: int, elem_bits: int,
                  part: "str | None" = None) -> BramEstimate:
    """Blocks for *n_banks* banks of *depth* elements each, and whether they are blocks at all.

    **What you supply:** the partitioning of your array -- how many banks (the ``ARRAY_PARTITION``
    factor), how deep each one is, and the element width.

    **What this supplies:** the block shape, the rounding, and an honest ``uncertain`` in the band
    where HLS may prefer distributed RAM.

    The **ceiling is the whole law**: each bank rounds up to whole blocks independently, so a bank
    shallower than one block still occupies a whole one.  Drop it and the bank count cancels, which
    is right only when banks are deeper than a block.
    """
    g = device_for(part).bram
    banks, d = int(n_banks), int(depth)
    per_elem, entries = bram_shape(elem_bits, part)
    blocks = banks * per_elem * math.ceil(d / entries)

    bank_bits = d * int(elem_bits)
    if bank_bits <= g.lutram_max_bank_bits:
        binding = "lutram"
    elif bank_bits >= g.bram_min_bank_bits:
        binding = "bram"
    else:
        binding = "uncertain"
    return BramEstimate(blocks=0 if binding == "lutram" else blocks, binding=binding,
                        entries_per_block=entries, blocks_per_element=per_elem)


def bram_count(n_banks: int, depth: int, elem_bits: int, part: "str | None" = None) -> int:
    """:func:`bram_estimate`'s block count.  ``uncertain`` still returns the block-RAM figure.

    Use :func:`bram_estimate` where the *binding* matters -- a caller that needs a number and a
    caller that needs a confidence want different things, and collapsing them is how an
    under-determined band turns into a confident wrong answer.
    """
    return bram_estimate(n_banks, depth, elem_bits, part).blocks
