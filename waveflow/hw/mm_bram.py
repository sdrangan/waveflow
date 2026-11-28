"""mm_bram.py — a memory-mapped window onto one port of a block RAM.

``plans/mm_slave_adaptor.md``, Stage 3: the pysim half of ``waveflow/build/rtl/mm_bram_port.v`` (with
the ``bram_t2p`` it drives) -- or of ``mm_bram_port_pipe.v``, the full-rate leaf
(``plans/bram_fullrate_port.md``; :attr:`MemSlaveBramWindow.bram_leaf`).  The bus reaches the memory
through :attr:`MemSlaveBramWindow.s_mem` (port A); the kernel's port B is :meth:`port_b_read` /
:meth:`port_b_write` -- untimed, as a BRAM port is one word per cycle at a fixed latency that the
kernel's own model accounts for.

Window: word *i* at local byte ``i * bytes_per_word``; words at or beyond ``nelem`` are outside the
memory (a write there is dropped, a read answers 0 and is recorded in :attr:`errors` -- SLVERR in RTL).
A word wider than 64 bits is held as 64-bit chunks, lowest first (bus words are ``(n, k)`` arrays, as
:class:`~waveflow.hw.memory.Memory` takes them); port B takes and gives Python ints.

**The bus side's timing comes from a record, and the record is a memory's.**  With :attr:`calib_dir`
set (a platform, or any directory with a ``modules/`` store), the window loads the timing record of
:func:`window_memory` ``(bram_leaf, mem_dwidth)`` -- a :class:`~waveflow.hw.memory.MemoryMod` (leaf
``mm_bram_port``) or :class:`~waveflow.hw.memory.FullRateMemoryMod` (``mm_bram_port_pipe``) of the same
width.  At RTL that memory's slot is this window's: ``axi_slave_front``, the same leaf, a ``bram_t2p``
(its port B tied idle, which port A does not see).  So the memory is calibrated in its own harness
(``waveflow.hw.harness.memory_harness``, ``waveflow.calib.fixtures.bram_window``) and the window reads
what was filed there: one source, the same record and the same loading path
(``waveflow.calib.mem_timing.load_mem_timing``).  A read or write then costs ``init + per_word * n``
cycles of the window's own clock, the slave serves one transaction at a time (``serialize``) and is
busy ``turnaround`` cycles after each, as the memory's is.  With no record (or no ``calib_dir``) the bus
side costs nothing, as before: the crossbar's settings are all the time a read takes.  The window has no
XSI gate of its own yet (its port B is not wired by ``system_top``: ``plans/bram_window_port_b.md``).

**Ordering lives in the adaptor, not here.**  Put this view and its doorbell (a queue or register bank)
behind one :class:`~waveflow.hw.mm_adaptor.MemSlaveAdaptor` and a doorbell written after the data
cannot reach the kernel before it; put them on separate bus ports and nothing orders them.  The RTL
pair is ``tests/build/test_mm_bram_order_xsi.py``; the pysim pair is ``tests/hw/test_mm_bram.py``.

Not yet joined to :class:`~waveflow.hw.bram.T2pBram` / ``BramIF`` (the kernel-side port B as a
declared endpoint, so a generated kernel can bind it) -- a follow-up the plan records.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, ClassVar

import numpy as np

from waveflow.hw.clock import Clock
from waveflow.hw.hw_module import HwModule, HwParam
from waveflow.hw.memif import MMIFSlave, Words
from waveflow.simulation.simobj import ProcessGen


#: The RTL leaves a BRAM window's port A may be (``MemSlaveBramWindow.bram_leaf``): ``mm_bram_port``
#: reads a word every two cycles (one read in flight, ``LAT + 1``); ``mm_bram_port_pipe`` a word every
#: cycle with a registered response, timing-closed (``plans/bram_fullrate_port.md``).
BRAM_WINDOW_LEAVES: tuple[str, ...] = ("mm_bram_port", "mm_bram_port_pipe")

#: The memory a window's timing record is measured on (:func:`window_memory`): its depth and clock.
#: Neither moves the RTL's cycles -- a BRAM read is the same cycles at any depth, and a record counts
#: cycles -- but both are part of a memory's identity, so they are fixed here, once: a window of any
#: depth and clock finds the one record of its leaf and width.
WINDOW_MEMORY_WORDS = 1024
WINDOW_MEMORY_FREQ = 250e6


def window_memory(bram_leaf: str, mem_dwidth: int, sim=None, *, name: str = "mem",
                  calib_dir: Any = None):
    """The memory whose RTL slot is a BRAM window's with leaf *bram_leaf* and width *mem_dwidth*: a
    :class:`~waveflow.hw.memory.MemoryMod` for ``mm_bram_port``, a
    :class:`~waveflow.hw.memory.FullRateMemoryMod` for ``mm_bram_port_pipe``, of
    :data:`WINDOW_MEMORY_WORDS` words at :data:`WINDOW_MEMORY_FREQ`.  What the calibration fixture
    harnesses (on its *sim*) and what a window looks its record up by (on a scratch one) -- the one
    definition both use."""
    from waveflow.hw.memory import AddrUnit, FullRateMemoryMod, MemoryMod
    from waveflow.simulation.simulation import Simulation

    if bram_leaf not in BRAM_WINDOW_LEAVES:
        raise ValueError(f"window_memory: bram_leaf must be one of {BRAM_WINDOW_LEAVES}, got {bram_leaf!r}")
    cls = FullRateMemoryMod if bram_leaf == "mm_bram_port_pipe" else MemoryMod
    assert getattr(cls, "BRAM_LEAF", "mm_bram_port") == bram_leaf
    return cls(name=name, sim=sim if sim is not None else Simulation(), word_size=int(mem_dwidth),
               inline=False, nwords_tot=WINDOW_MEMORY_WORDS, clk=Clock(freq=WINDOW_MEMORY_FREQ),
               addr_unit=AddrUnit.byte, calib_dir=calib_dir)


@dataclass
class MemSlaveBramWindow(HwModule):
    """A BRAM of ``nelem`` words: port A on the bus (:attr:`s_mem`), port B for the kernel."""

    #: What a bus master reaches this view as -- see :class:`~waveflow.hw.mm_host.MemSlaveMap`.
    view_kind: ClassVar[str] = "bram"

    mem_dwidth: HwParam[int] = 64
    nelem: HwParam[int] = 512
    #: Port A's RTL leaf, one of :data:`BRAM_WINDOW_LEAVES` (its ``BramView`` in
    #: ``waveflow.build.mm_adaptor_gen.BRAM_VIEWS``).  A build input (``build_fields``): the two leaves
    #: are different hardware with different timing, so each has its own identity and its own records.
    bram_leaf: str = "mm_bram_port"
    build_fields: ClassVar[tuple[str, ...]] = ("bram_leaf",)
    window: int = 4096
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))
    #: Where the bus side's timing record is looked up (see the module docstring): the record of
    #: :func:`window_memory` ``(bram_leaf, mem_dwidth)``.  ``None``: the bus side costs nothing.
    calib_dir: Any = None

    #: Run-time state, not structure (``waveflow.calib.phases.module_identity``): the contents, the
    #: errors a run recorded, and the timing a run loaded.
    __wf_identity_exclude__: ClassVar[tuple[str, ...]] = ("mem", "errors", "_mem_timing")

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.bram_leaf not in BRAM_WINDOW_LEAVES:
            raise ValueError(f"{type(self).__name__}: bram_leaf must be one of {BRAM_WINDOW_LEAVES}, "
                             f"got {self.bram_leaf!r}")
        dw, n = int(self.mem_dwidth), int(self.nelem)
        self._bpw = dw // 8
        if n < 1 or n & (n - 1):
            raise ValueError(f"{type(self).__name__}: nelem must be a power of two, got {n} "
                             f"(bram_t2p addresses mem[addr[AW-1:0]]; any other depth aliases)")
        if n * self._bpw > int(self.window):
            raise ValueError(f"{self.name}: {n} words of {self._bpw} bytes exceed the "
                             f"{int(self.window)}-byte window")
        self._dt = np.dtype(np.uint32) if dw <= 32 else np.dtype(np.uint64)
        #: 64-bit chunks a word: 1 up to 64 bits, else ``ceil(dw / 64)`` (a word is a row).
        self._k = 1 if dw <= 64 else -(-dw // 64)
        self.mem = np.zeros(n if self._k == 1 else (n, self._k), dtype=self._dt)
        self.s_mem = MMIFSlave(name=f"{self.name}_s_mem", sim=self.sim, bitwidth=dw,
                               rx_write_proc=self._on_write, rx_read_proc=self._on_read,
                               peek_read=self._peek)
        self.add_endpoint(self.s_mem)
        self.errors: list[tuple[float, str, int]] = []
        self._mem_timing: dict | None = None

    def pre_sim(self) -> None:
        super().pre_sim()
        self._apply_stored_timing()

    def _apply_stored_timing(self) -> None:
        """Load the bus side's timing: the record of :func:`window_memory` of this leaf and width
        (looked up on a scratch simulation), through the path the memory itself loads it by."""
        from waveflow.calib.mem_timing import apply_mem_timing, load_mem_timing

        self._mem_timing = None
        if self.calib_dir is None:
            return
        found = load_mem_timing(self.calib_dir, window_memory(self.bram_leaf, int(self.mem_dwidth)))
        if found is not None:
            self._mem_timing = found[0]
            apply_mem_timing(self.s_mem, *found)

    def _access_delay(self, nwords: int, kind: str) -> float:
        """The bus side's access time (seconds) for *nwords* words: the record's cycles on this
        window's clock, or nothing without one."""
        from waveflow.calib.mem_timing import access_cycles

        if self._mem_timing is None:
            return 0.0
        return access_cycles(self._mem_timing, nwords, kind) / self.clk.freq

    # -- kernel side: port B -------------------------------------------------------------------------
    def port_b_read(self, i: int) -> int:
        w = self.mem[int(i) % int(self.nelem)]
        if self._k == 1:
            return int(w)
        return sum(int(c) << (64 * j) for j, c in enumerate(w))

    def port_b_write(self, i: int, value: int) -> None:
        if self._k == 1:
            self.mem[int(i) % int(self.nelem)] = value
        else:
            v = int(value)
            self.mem[int(i) % int(self.nelem)] = [(v >> (64 * j)) & ((1 << 64) - 1)
                                                  for j in range(self._k)]

    # -- bus side: port A ----------------------------------------------------------------------------
    def _rows(self, words: Words) -> np.ndarray:
        a = np.asarray(words, dtype=self._dt)
        return a.reshape(-1) if self._k == 1 else a.reshape(-1, self._k)

    def _on_write(self, words: Words, local_addr: int) -> ProcessGen[None]:
        rows = self._rows(words)
        yield self.timeout(self._access_delay(len(rows), "write"))
        w0 = int(local_addr) // self._bpw
        for i, w in enumerate(rows):
            if w0 + i < int(self.nelem):
                self.mem[w0 + i] = w
            else:
                self.errors.append((self.env.now, "outside_memory", int(local_addr) + i * self._bpw))

    def _on_read(self, nwords: int, local_addr: int) -> ProcessGen[Words]:
        yield self.timeout(self._access_delay(int(nwords), "read"))
        out = self._peek(nwords, local_addr)
        w0 = int(local_addr) // self._bpw
        for i in range(int(nwords)):
            if w0 + i >= int(self.nelem):
                self.errors.append((self.env.now, "outside_memory", int(local_addr) + i * self._bpw))
        return out

    def _peek(self, nwords: int, local_addr: int) -> Words:
        w0 = int(local_addr) // self._bpw
        out = np.zeros(int(nwords) if self._k == 1 else (int(nwords), self._k), dtype=self._dt)
        for i in range(int(nwords)):
            if w0 + i < int(self.nelem):
                out[i] = self.mem[w0 + i]
        return out


__all__ = ["BRAM_WINDOW_LEAVES", "MemSlaveBramWindow", "WINDOW_MEMORY_FREQ", "WINDOW_MEMORY_WORDS",
           "window_memory"]
