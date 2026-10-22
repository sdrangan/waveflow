"""config.py — :class:`VecUnitConfig`, the build-time description of one vector unit.

Every size is a build parameter, and so is every **format**: each vector register group, each scalar
and each queue has its own fixed-point format (``plans/vecunit_new.md`` D4).  An application chooses
them -- the unit knows nothing about any application.

* **Vector register file.** ``NG`` groups of ``L`` members of ``VLMAX`` complex elements; group ``g``
  in format ``group_formats[g]``.  Physically ``NB`` banks per lane: group ``g`` lives in bank
  ``g % NB`` (D2), and a pass may read one group of each bank and write one group of each bank.
* **Scalar file.** ``NS`` per-member real scalars ``s[l]``; scalar ``j`` in ``scalar_formats[j]``.
* **Queues.** ``NQI`` input and ``NQO`` output queues; a block holds one ``k x n`` matrix
  (``k <= Kmax``, ``n <= Nmax``) of complex elements in the queue's format, ``ceil(n / L)`` lane
  groups a row (D5: ``L`` need not divide ``n``; the last group's spare lanes are zero).
* **Slots.** A pass runs at most ``S`` chained instructions; ``slot_caps[s]`` says which operation
  classes slot ``s`` has hardware for (:class:`Cap`), every class by default.
* **Divides.** ``wf.sdivz`` widens its dividend by ``g_div`` fraction bits first.
* **Ports.** ``n_in_ports`` input and ``n_out_ports`` output streams of ``word_bits``-bit words, each
  value a memory element ``lane_bits`` wide (:func:`waveflow.linalg.formats.mem_format`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntFlag

from waveflow.linalg.formats import DEFAULT_LANE_BITS, mem_format
from waveflow.linalg.lanes import WORD_BITS_SUPPORTED
from waveflow.utils.fixputils import Format

#: Instruction fields that name a register, a scalar, a queue or a port are 8 bits wide; ``VL``,
#: ``k``, ``n``, a column group and a stride are 12 bits.
MAX_INDEX = 256
MAX_IMM = 4096


class Cap(IntFlag):
    """Operation classes a slot may have hardware for."""

    MOVE = 1  # vmv.v.i, vmv.v.v, vadd.vv, vsub.vv: no multiplier
    MUL = 2  # vmul, vmacc, vnmsac, vmadd and their .vs, conjugate and broadcast forms
    DOT = 4  # wf.vdotr
    LOAD = 8  # wf.vlse
    STORE = 16  # wf.vsse
    ALL = 31


@dataclass(frozen=True)
class VecUnitConfig:
    """One vector unit's build parameters (see the module doc)."""

    group_formats: tuple[Format, ...]
    scalar_formats: tuple[Format, ...]
    in_queue_formats: tuple[Format, ...]
    out_queue_formats: tuple[Format, ...]
    L: int = 4
    VLMAX: int = 8
    Kmax: int = 8
    Nmax: int = 32
    S: int = 3
    NB: int = 4
    slot_caps: tuple[int, ...] = field(default=())
    g_div: int = 0
    word_bits: int = 64
    lane_bits: int = DEFAULT_LANE_BITS
    n_in_ports: int = 1
    n_out_ports: int = 1

    def __post_init__(self) -> None:
        for name in ("group_formats", "scalar_formats", "in_queue_formats", "out_queue_formats",
                     "slot_caps"):
            object.__setattr__(self, name, tuple(getattr(self, name)))
        if not self.slot_caps:
            object.__setattr__(self, "slot_caps", (int(Cap.ALL),) * int(self.S))
        L, NB = int(self.L), int(self.NB)
        problems = []
        if L < 1 or L & (L - 1):
            problems.append(f"the lane count L = {L} is not a power of two")
        if not 1 <= self.VLMAX < MAX_IMM:
            problems.append(f"VLMAX = {self.VLMAX} is not in 1..{MAX_IMM - 1}")
        if not (1 <= self.Kmax < MAX_IMM and 1 <= self.Nmax < MAX_IMM):
            problems.append(f"Kmax, Nmax = {self.Kmax}, {self.Nmax} are not in 1..{MAX_IMM - 1}")
        for name, n in (("NG", self.NG), ("NS", self.NS), ("NQI", self.NQI), ("NQO", self.NQO),
                        ("n_in_ports", self.n_in_ports), ("n_out_ports", self.n_out_ports)):
            if not 1 <= n <= MAX_INDEX:
                problems.append(f"{name} = {n} is not in 1..{MAX_INDEX}")
        if NB < 1 or self.NG % NB:
            problems.append(f"NB = {NB} banks must divide NG = {self.NG}")
        if self.S < 1 or len(self.slot_caps) != self.S:
            problems.append(f"S = {self.S} slots, but {len(self.slot_caps)} slot_caps")
        if any(not 0 < int(c) <= int(Cap.ALL) for c in self.slot_caps):
            problems.append(f"slot_caps {self.slot_caps}: each a nonzero Cap mask")
        if self.g_div < 0:
            problems.append(f"g_div = {self.g_div} is negative")
        if self.word_bits not in WORD_BITS_SUPPORTED:
            problems.append(f"word_bits = {self.word_bits} is not one of {WORD_BITS_SUPPORTED}")
        for name in ("group_formats", "scalar_formats", "in_queue_formats", "out_queue_formats"):
            for j, f in enumerate(getattr(self, name)):
                if not isinstance(f, Format) or not f.signed:
                    problems.append(f"{name}[{j}] = {f}: every format must be a signed Format")
        for name in ("in_queue_formats", "out_queue_formats"):
            for j, f in enumerate(getattr(self, name)):
                if isinstance(f, Format) and f.W > self.lane_bits:
                    problems.append(f"{name}[{j}]: {f.W} bits do not fit a {self.lane_bits}-bit "
                                    "lane")
        if problems:
            raise ValueError("VecUnitConfig: " + "; ".join(problems))

    # -- sizes -----------------------------------------------------------------------------------
    @property
    def NG(self) -> int:
        return len(self.group_formats)

    @property
    def NS(self) -> int:
        return len(self.scalar_formats)

    @property
    def NQI(self) -> int:
        return len(self.in_queue_formats)

    @property
    def NQO(self) -> int:
        return len(self.out_queue_formats)

    @property
    def NGQ(self) -> int:
        """Lane groups a queue block row holds: ``ceil(Nmax / L)``."""
        return -(-int(self.Nmax) // int(self.L))

    def groups_per_row(self, n: int) -> int:
        """Lane groups a row of an ``n``-column block takes: ``ceil(n / L)``, the stride of
        ``wf.vlse`` / ``wf.vsse`` on it."""
        return -(-int(n) // int(self.L))

    def bank(self, group: int) -> int:
        """The bank group ``group`` lives in."""
        return int(group) % int(self.NB)

    # -- formats ---------------------------------------------------------------------------------
    @property
    def vector_formats(self) -> tuple[Format, ...]:
        """The distinct formats of the vector groups and queues, in first-use order: the vector
        format ids the hardware muxes on."""
        fmts = (*self.group_formats, *self.in_queue_formats, *self.out_queue_formats)
        return tuple(dict.fromkeys(fmts))

    @property
    def scalar_format_set(self) -> tuple[Format, ...]:
        """The distinct scalar formats, in first-use order."""
        return tuple(dict.fromkeys(self.scalar_formats))

    def vector_union(self) -> Format:
        """A format holding every vector and queue format exactly: the most fraction bits and the
        most integer bits of any (a slot's operands convert to it on read)."""
        return _union(self.vector_formats)

    def scalar_union(self) -> Format:
        return _union(self.scalar_format_set)

    def mem_formats(self) -> tuple[Format, ...]:
        """The memory element formats of the queues (what travels on the ports)."""
        return tuple(dict.fromkeys(mem_format(f, self.lane_bits)
                                   for f in (*self.in_queue_formats, *self.out_queue_formats)))

    def has_cap(self, slot: int, cap: Cap) -> bool:
        return bool(int(self.slot_caps[slot]) & int(cap))


def _union(fmts) -> Format:
    frac = max(f.frac_bits for f in fmts)
    ints = max(f.int_bits for f in fmts)
    return Format(frac + ints, ints, True)
