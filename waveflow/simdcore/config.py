"""config.py — :class:`SimdCoreConfig`, the build-time description of one SIMD core.

``plans/simdcore.md`` and its Stage 0 records (``plans/simdcore_records.md``).  Every size is a build
parameter; nothing here names an application.

* **Lanes.** ``L`` identical lanes of type ``lane``: ``"cplx4"`` (four 16-bit multipliers a 32-bit
  lane word; the default, for complex-heavy glue) or ``"real2"`` (two; real-heavy glue).  Element ``i``
  of a register lives in lane ``i mod L``.
* **Vector registers.** 32 registers of ``vlen`` bits (RVV ``VLEN``); ``SEW`` in ``sews``; ``LMUL`` 1.
* **Windows** (D10).  Runs of consecutive registers the loader fills (``"in"``) or the storer drains
  (``"out"``), each with two physical copies; ``wf.qpop`` / ``wf.qpush`` swap them.  A block is a
  ``k x n`` matrix of ``sew``-bit elements, row-major on the stream; column ``j`` is register
  ``base + j``, row ``i`` its element ``i``.
* **Data memory.** ``dm_bytes`` bytes, word-interleaved over ``L`` banks; the top ``4 * lut_entries``
  bytes are the LUT region, the only target of ``vluxei`` (D5).
* **Optional memory paths** (records D13), off by default -- features are build options: ``lut``
  (``vluxei`` on the LUT region), ``strided`` (``vlse`` / ``vsse`` / ``vlseg2`` / ``vsseg2``) and
  ``scalar_mem`` (``lh lhu lw sh sw``).  Off, they are illegal.  Unit-stride ``vle`` / ``vse`` are
  always there; their base address must be a multiple of ``4·L`` bytes (element ``i`` in lane
  ``i mod L``'s bank).
* **Hardware rules** (D13): no window holds ``v0`` (the mask register); an input window's registers
  are read-only (an instruction writing one is illegal).
* **Complex ops** (D7).  ``cplx_ops``: the ``wf.`` complex instructions exist; off, they are illegal.
* **Timing** (D2; the interpreter's cycle model, not a calibration).  ``depth`` is the lane pipeline's
  read-to-write distance ``D``; ``scalar_ii`` the scalar unit's cycles an instruction (vector ones
  included: it decodes them); ``div_cycles`` the iterative scalar divide; ``vq_depth`` the scalar ->
  vector instruction queue; ``tree_depth`` the reduction tree's extra latency (``log2 L`` by default).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import log2

LANE_TYPES = ("cplx4", "real2")


@dataclass(frozen=True)
class Window:
    """A run of ``n`` consecutive vector registers from ``base`` the loader or storer moves."""

    name: str
    base: int
    n: int
    direction: str  # "in" or "out"
    sew: int = 32
    port: int = 0  # the loader's (or storer's) stream port; windows on one port share its words
    #: Physical copies.  2 (ping-pong): after ``wf.qpush`` the registers are the other copy,
    #: unspecified until written.  1 (output windows only): ``wf.qpush`` keeps the values -- the
    #: storer reads the one copy -- and a write to the window waits until the storer has drained it.
    copies: int = 2

    def __post_init__(self) -> None:
        if self.direction not in ("in", "out"):
            raise ValueError(f"window {self.name}: direction {self.direction!r} is not 'in' or 'out'")
        if self.copies not in (1, 2) or (self.copies == 1 and self.direction != "out"):
            raise ValueError(f"window {self.name}: copies {self.copies} (1 only for an output window)")
        if not (0 <= self.base and self.base + self.n <= 32 and self.n >= 1):
            raise ValueError(f"window {self.name}: registers v{self.base}..v{self.base + self.n - 1} out of range")

    @property
    def regs(self) -> range:
        return range(self.base, self.base + self.n)


@dataclass(frozen=True)
class SimdCoreConfig:
    """One SIMD core's build parameters (see the module doc)."""

    L: int = 2
    lane: str = "cplx4"
    vlen: int = 2048
    sews: tuple[int, ...] = (16, 32)
    windows: tuple[Window, ...] = field(default=())
    dm_bytes: int = 64 * 1024
    lut_entries: int = 256
    imem_words: int = 4096
    cplx_ops: bool = True
    div: bool = True
    vdiv: bool = False  # vdiv / vdivu / vrem / vremu: a pipelined divider a lane (records D6)
    vdiv_latency: int = 36  # its read-to-write distance, in place of ``depth``
    lut: bool = False  # vluxei on the LUT region (D5, D13)
    strided: bool = False  # vlse / vsse / vlseg2 / vsseg2 (D13)
    scalar_mem: bool = False  # lh lhu lw sh sw (D13)
    word_bits: int = 64  # a stream port's word: the loader / storer move one a cycle
    # timing (D2)
    depth: int = 9  # witness 6 (2025.1): the flattened loop's depth at L 2-16
    div_cycles: int = 34
    vq_depth: int = 8
    issue_latency: int = 1
    scalar_ii: int = 3  # witness 5 (2025.1): the scalar unit issues an instruction every 3 cycles
    tree_depth: int | None = None

    def __post_init__(self) -> None:
        if self.L < 1 or self.L & (self.L - 1):
            raise ValueError(f"L = {self.L}: a power of two")
        if self.lane not in LANE_TYPES:
            raise ValueError(f"lane = {self.lane!r}: one of {LANE_TYPES}")
        if self.vlen < 32 or self.vlen & (self.vlen - 1):
            raise ValueError(f"vlen = {self.vlen}: a power of two, at least 32 (Zve32x)")
        if not set(self.sews) <= {16, 32} or not self.sews:
            raise ValueError(f"sews = {self.sews}: a non-empty subset of (16, 32)")
        if self.vlen // 32 < self.L:
            raise ValueError(f"vlen = {self.vlen} gives fewer e32 elements than L = {self.L} lanes")
        used: dict[int, str] = {}
        for w in self.windows:
            if w.sew not in self.sews:
                raise ValueError(f"window {w.name}: sew {w.sew} not in {self.sews}")
            if w.base == 0:
                raise ValueError(f"window {w.name}: v0 is the mask register, in no window (D13)")
            for r in w.regs:
                if r in used:
                    raise ValueError(f"window {w.name}: v{r} already in window {used[r]}")
                used[r] = w.name
        if len({w.name for w in self.windows}) != len(self.windows):
            raise ValueError("window names must be unique")
        if self.dm_bytes % (4 * self.L) or self.dm_bytes < 4 * self.lut_entries:
            raise ValueError(f"dm_bytes = {self.dm_bytes}: a multiple of 4 L, holding the LUT region")

    # --- derived -----------------------------------------------------------------------------------

    @property
    def vlenb(self) -> int:
        return self.vlen // 8

    def vlmax(self, sew: int) -> int:
        return self.vlen // sew

    @property
    def lut_base(self) -> int:
        return self.dm_bytes - 4 * self.lut_entries

    @property
    def multipliers(self) -> int:
        """16-bit multipliers (DSPs) in the lanes."""
        return self.L * (4 if self.lane == "cplx4" else 2)

    @property
    def tree(self) -> int:
        return self.tree_depth if self.tree_depth is not None else max(1, int(log2(self.L)))

    def window(self, q: int | str) -> Window:
        if isinstance(q, str):
            for w in self.windows:
                if w.name == q:
                    return w
            raise KeyError(q)
        return self.windows[q]

    @property
    def in_window_regs(self) -> frozenset[int]:
        """The registers of the input windows: read-only (D13)."""
        return frozenset(r for w in self.windows if w.direction == "in" for r in w.regs)

    def window_index(self, name: str) -> int:
        for i, w in enumerate(self.windows):
            if w.name == name:
                return i
        raise KeyError(name)

    def elems_per_lane_cycle(self, sew: int, cplx: bool = False) -> float:
        """Elements a lane retires a cycle: a 32-bit lane word holds two ``e16`` or one ``e32``;
        a ``real2`` lane takes two cycles for a complex multiply."""
        if cplx:
            return 1.0 if self.lane == "cplx4" else 0.5
        return 32 / sew
