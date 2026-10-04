"""mm_queue.py — the memory-mapped queue windows: a bus slave on one side, a stream on the other.

``plans/mm_slave_adaptor.md``, Stage 1.  The pysim halves of the two hand-written RTL leaves:

* :class:`MemSlaveWStream` (RTL ``mm_queue_in.v``) — bus **writes** into its window push a stream to
  the kernel.  The mirror of :class:`~waveflow.hw.mem_stream.MemWStream`: that one is a bus master
  the kernel commands; this one is a bus slave other masters write into.
* :class:`MemSlaveRStream` (RTL ``mm_queue_out.v``) — the kernel's stream is drained by bus
  **reads**.

Both are plain :class:`~waveflow.hw.hw_module.HwModule` s, not ``FreeRunMod`` s: there is no firing
loop — they act only when a bus transaction arrives — and they are realized as RTL beside the Vitis
top, never as an HLS task (Vitis has no AXI4-full slave).

**Semantics, identical to the RTL** (the gate is ``tests/build/test_mm_queue_xsi.py``; the pysim
twin of its scenario is ``tests/hw/test_mm_queue.py``):

* **Queue in, writes** — the window's lower half pushes.  Framing is in-band: each packet is
  ``[len | data x len]`` (the header's low 32 bits); the header is consumed, the data words are pushed,
  and a packet may span any number of bursts.  ``len = 0`` is an empty packet.  A write to the
  **upper half** sets the interrupt threshold and pushes nothing (``plans/mm_irq.md`` D2).
* **Queue in, reads** — any address: the **vacancy** (free slots, ``0..depth``).  No side effects.
* **Queue out, reads** — the window's lower half pops data; the upper half returns the
  **occupancy**.  Popping an empty queue returns 0 and is an error (SLVERR in RTL), never a wait.
* **Queue out, writes** — to the upper half: the interrupt threshold.  Elsewhere: dropped.
* **Interrupts** — each view drives :attr:`m_irq` (an :class:`~waveflow.hw.irq.IrqIFSource`): queue in
  while ``vacancy >= threshold``, queue out while ``occupancy >= threshold``.  Threshold 0 (the reset
  value) disables it, so a design that never writes one sees nothing new.

**What pysim models coarser than RTL.**  A pysim stream moves whole *bursts*, so a packet reaches the
kernel as one burst once its last word has been written (in RTL the words stream through as they
arrive).  For a packet carried in one AXI burst — the common case — the two agree.  Back-pressure is
burst-granular for the same reason: :meth:`StreamIF.write` waits until the packet fits (or the queue
is empty, for a packet larger than it), and that wait holds the crossbar's write channel, so the
writing master stalls as WREADY-low stalls it in RTL.

**Errors** have no channel in the pysim crossbar (``MMIFMaster.read`` returns words only), so each
module records them in :attr:`errors` as ``(time, kind, local_addr)`` and returns the RTL's data.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import ClassVar

import numpy as np

from waveflow.hw.clock import Clock
from waveflow.hw.hw_module import HwModule, HwParam
import simpy

from waveflow.hw.interface import StreamIFMaster, StreamIFSlave
from waveflow.hw.irq import IrqIFSource
from waveflow.hw.memif import MMIFSlave, Words
from waveflow.simulation.simobj import ProcessGen


def _dtype(dw: int) -> np.dtype:
    return np.dtype(np.uint32) if dw <= 32 else np.dtype(np.uint64)


class _NotifyingStore(simpy.Store):
    """A ``simpy.Store`` that calls *on_change* after every put and take -- so a queue view can drive
    its interrupt the instant its count changes, without polling the count inside the simulation."""

    def __init__(self, env, on_change, items=()):
        super().__init__(env)
        self.items.extend(items)
        self._on_change = on_change

    def _do_put(self, event):
        out = super()._do_put(event)
        self._on_change()
        return out

    def _do_get(self, event):
        out = super()._do_get(event)
        self._on_change()
        return out


def _check_geometry(name: str, depth: int, window: int) -> None:
    if depth < 2 or depth & (depth - 1):
        raise ValueError(f"{name}: depth must be a power of two >= 2, got {depth}")
    if window < 4096 or window & (window - 1):
        raise ValueError(f"{name}: window must be a power of two >= 4 KB (one AXI burst may not "
                         f"cross a 4 KB boundary), got {window}")


@dataclass
class MemSlaveWStream(HwModule):
    """A queue-in window: bus writes ``[len | data...]`` -> packets on :attr:`m_out`.

    Endpoints: ``s_mem`` (:class:`~waveflow.hw.memif.MMIFSlave`, bind to a crossbar slave port) and
    ``m_out`` (:class:`~waveflow.hw.interface.StreamIFMaster`, packets with TLAST).  The stream
    channel ``m_out`` is bound to IS the FIFO: its ``depth`` must equal :attr:`depth`, checked when
    the simulation starts.
    """

    #: What a bus master reaches this view as -- see :class:`~waveflow.hw.mm_host.MemSlaveMap`.
    view_kind: ClassVar[str] = "queue_in"

    mem_dwidth: HwParam[int] = 64
    depth: HwParam[int] = 512
    window: int = 4096
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        _check_geometry(type(self).__name__, int(self.depth), int(self.window))
        dw = int(self.mem_dwidth)
        self.s_mem = MMIFSlave(name=f"{self.name}_s_mem", sim=self.sim, bitwidth=dw,
                               rx_write_proc=self._on_write, rx_read_proc=self._on_read,
                               peek_read=self._peek)
        self.m_out = StreamIFMaster(name=f"{self.name}_m_out", sim=self.sim, bitwidth=dw,
                                    has_tlast=True)
        #: The interrupt: high while ``vacancy >= irq_threshold`` (``plans/mm_irq.md`` D2).
        self.m_irq = IrqIFSource(name=f"{self.name}_m_irq", sim=self.sim)
        for ep in (self.s_mem, self.m_out, self.m_irq):
            self.add_endpoint(ep)
        #: Written by the bus at the upper half of the window; 0 disables the interrupt.
        self.irq_threshold = 0
        self._dt = _dtype(dw)
        self._left = 0                      # data words the current packet still owes; 0 = header next
        self._pkt: list[int] = []
        #: ``(time, kind, local_addr)`` for every refused access (none occur on this view today; the
        #: list exists so both queue views expose the same record).
        self.errors: list[tuple[float, str, int]] = []
        #: Packets pushed, as word counts (observability; the RTL's TLAST positions).
        self.packets: list[int] = []
        #: Packets that arrived with too little room and so stalled the bus until the kernel drained
        #: the queue.  A writer that respects credit (``plans/mm_credit_stream.md``) keeps it zero.
        self.nstall = 0

    # -- the FIFO is the stream channel ------------------------------------------------------------
    def _fifo(self):
        iface = self.m_out.interface
        if iface is None:
            raise RuntimeError(f"{self.name}: m_out is not bound to a stream")
        if iface.depth != int(self.depth):
            raise ValueError(f"{self.name}: the stream bound to m_out has depth {iface.depth}, but the "
                             f"queue's depth is {int(self.depth)}.  That channel IS the FIFO; make "
                             f"the two agree.")
        return iface.endpoints["slave"]

    def occupancy(self) -> int:
        """Data words queued and not yet taken by the kernel."""
        ep = self._fifo()
        pending = sum(len(b) for b in ep.data_buffer.items)
        return int(pending)

    def vacancy(self) -> int:
        """Free slots.  Clamped at 0: a pysim packet longer than the queue is handed over whole once
        the queue drains (burst-granular back-pressure), which RTL -- word by word -- never does."""
        return max(0, int(self.depth) - self.occupancy())

    def _update_irq(self) -> None:
        t = int(self.irq_threshold)
        self.m_irq.set(t > 0 and self.vacancy() >= t)

    def pre_sim(self) -> None:
        super().pre_sim()
        ep = self._fifo()                   # fail at start, not at the first write
        # Hear every put and take on the FIFO -- the kernel draining it changes the vacancy.
        ep.data_buffer = _NotifyingStore(self.env, self._update_irq, ep.data_buffer.items)

    # -- bus side ----------------------------------------------------------------------------------
    def _on_write(self, words: Words, local_addr: int) -> ProcessGen[None]:
        bpw = int(self.mem_dwidth) // 8
        for i, w in enumerate(np.asarray(words).tolist()):
            if int(local_addr) + i * bpw >= int(self.window) // 2:
                self.irq_threshold = int(w)          # the control half: the interrupt threshold
                self._update_irq()
                continue
            if self._left == 0:
                self._left = int(w) & 0xFFFF_FFFF
                self._pkt = []
                continue
            self._pkt.append(int(w))
            self._left -= 1
            if self._left == 0:
                pkt = np.asarray(self._pkt, dtype=self._dt)
                self._pkt = []
                self.packets.append(len(pkt))
                if self.vacancy() < len(pkt):
                    self.nstall += 1
                # Early-anchored: in RTL the words cut through to the stream as each beat arrives,
                # so the packet is delivered as its LAST word lands -- now -- not one packet-length
                # later.  A plain write() would charge the length a second time, after the bus burst
                # that already paid it.  (Room in the FIFO is still waited for: that is the stall.)
                yield from self.m_out.write_pipelined(
                    pkt, t_out_start=self.env.now - len(pkt) * self.clk.period)
        yield self.env.timeout(0)

    def _on_read(self, nwords: int, local_addr: int) -> ProcessGen[Words]:
        yield self.env.timeout(0)
        return self._peek(nwords, local_addr)

    def _peek(self, nwords: int, local_addr: int) -> Words:
        return np.full(int(nwords), self.vacancy(), dtype=self._dt)


@dataclass
class MemSlaveRStream(HwModule):
    """A queue-out window: the kernel's stream on :attr:`s_in` -> bus reads.

    Endpoints: ``s_mem`` (:class:`~waveflow.hw.memif.MMIFSlave`) and ``s_in``
    (:class:`~waveflow.hw.interface.StreamIFSlave`, the kernel writes to it).  Local addresses below
    ``window / 2`` pop data; at or above it, a read returns the occupancy.

    ``s_in`` is **unframed** (``has_tlast=False``): the bus side has no way to see a packet
    boundary (the RTL drops TLAST), so the honest declaration is that this queue does not carry
    one, and a kernel feeding it declares its own end ``has_tlast=False`` to match.
    """

    view_kind: ClassVar[str] = "queue_out"

    mem_dwidth: HwParam[int] = 64
    depth: HwParam[int] = 512
    window: int = 4096
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        _check_geometry(type(self).__name__, int(self.depth), int(self.window))
        dw = int(self.mem_dwidth)
        self.s_mem = MMIFSlave(name=f"{self.name}_s_mem", sim=self.sim, bitwidth=dw,
                               rx_write_proc=self._on_write, rx_read_proc=self._on_read,
                               peek_read=self._peek)
        self.s_in = StreamIFSlave(name=f"{self.name}_s_in", sim=self.sim, bitwidth=dw,
                                  has_tlast=False)
        # Hear every word the kernel puts (and every take) -- they change the occupancy.
        self.s_in.data_buffer = _NotifyingStore(self.env, self._update_irq)
        #: The interrupt: high while ``occupancy >= irq_threshold`` (``plans/mm_irq.md`` D2).
        self.m_irq = IrqIFSource(name=f"{self.name}_m_irq", sim=self.sim)
        for ep in (self.s_mem, self.s_in, self.m_irq):
            self.add_endpoint(ep)
        #: Written by the bus at the status half of the window; 0 disables the interrupt.
        self.irq_threshold = 0
        self._dt = _dtype(dw)
        self._held: deque[int] = deque()    # words of a burst already taken off s_in, not yet read
        self.errors: list[tuple[float, str, int]] = []

    def status_addr(self) -> int:
        """First local address of the status half."""
        return int(self.window) // 2

    def occupancy(self) -> int:
        """Words readable now: those held plus every burst waiting on ``s_in``."""
        return len(self._held) + int(sum(len(b) for b in self.s_in.data_buffer.items))

    def _update_irq(self) -> None:
        if not hasattr(self, "m_irq"):
            return                           # during construction
        t = int(self.irq_threshold)
        self.m_irq.set(t > 0 and self.occupancy() >= t)

    def _on_write(self, words: Words, local_addr: int) -> ProcessGen[None]:
        # The status half takes the interrupt threshold; anything else is dropped, as in RTL.
        bpw = int(self.mem_dwidth) // 8
        for i, w in enumerate(np.asarray(words).tolist()):
            if int(local_addr) + i * bpw >= self.status_addr():
                self.irq_threshold = int(w)
        self._update_irq()
        yield self.env.timeout(0)

    def _on_read(self, nwords: int, local_addr: int) -> ProcessGen[Words]:
        if local_addr >= self.status_addr():
            yield self.env.timeout(0)
            return self._peek(nwords, local_addr)
        out = np.zeros(int(nwords), dtype=self._dt)
        for i in range(int(nwords)):
            if not self._held and self.s_in.data_buffer.items:
                # A burst is already waiting, so this get() does not block: it only moves the words
                # from the channel to here (retiring them frees the kernel's side of the FIFO).
                # Unframed, so get() needs a count: take exactly the waiting burst, whole.
                burst = yield from self.s_in.get(nwords_max=len(self.s_in.data_buffer.items[0]))
                self._held.extend(int(w) for w in np.asarray(burst).tolist())
            if self._held:
                out[i] = self._held.popleft()
            else:
                self.errors.append((self.env.now, "pop_empty", int(local_addr)))
        self._update_irq()
        yield self.env.timeout(0)
        return out

    def _peek(self, nwords: int, local_addr: int) -> Words:
        if local_addr < self.status_addr():
            raise ValueError(f"{self.name}: the data half cannot be peeked (a read there pops); "
                             f"poll the status half at local 0x{self.status_addr():x} instead")
        return np.full(int(nwords), self.occupancy(), dtype=self._dt)


__all__ = ["MemSlaveWStream", "MemSlaveRStream"]
