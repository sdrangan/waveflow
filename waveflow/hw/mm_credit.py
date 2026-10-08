"""mm_credit.py — a :class:`~waveflow.hw.reverse_stream.CreditStreamIF` routed over a shared bus.

``plans/mm_credit_stream.md`` Stage 1.  A kernel that writes another kernel's queue over a bus must
never write into a full queue: a stalled bus write holds the crossbar path and the target's adaptor
front, which blocks every view behind it for every master -- including the reads that would drain
the queue.  Credit-based flow control is the standard answer, and the repo already has it:
:class:`~waveflow.hw.reverse_stream.CreditStreamIF`, a forward stream plus a reverse stream of
**cumulative** words consumed.  This module changes only its transport:

=====================  ==============================  ==============================================
                       direct (``CreditStreamIF``)     routed (:class:`MmCreditStreamIF`)
=====================  ==============================  ==============================================
forward                stream into the consumer FIFO   producer's :class:`MmStreamWriter` -> the
                                                       consumer's **queue-in view** (``[len|data]``)
reverse                stream of cumulative counts     consumer's :class:`MmStreamWriter` (credit
                                                       mode) -> the producer's **credit-in view**
receiver's buffer      the stream FIFO                 the queue-in view's FIFO, the same ``depth``
kernel endpoints       ``CreditStreamMasterIF`` /      **the same**
                       ``CreditStreamSlaveIF``
=====================  ==============================  ==============================================

The kernels declare the two views like any other (``QueueIn`` on the consumer's credit port,
:class:`~waveflow.hw.mm_device.CreditIn` on the producer's), so ``build_mm_device`` joins each view
to the right half of the credit endpoint.  :class:`MmCreditStreamIF` then binds the two endpoints,
builds the two writers, and -- once the bus is placed -- is told where the views are (:meth:`place`).

**Why the credit-in view may overwrite** (D2).  A register bank's COMMIT waits while the kernel has
not taken the previous config, which stalls the bus.  A credit value is cumulative, so the newest one
is the whole truth: the view keeps only the latest and hands it to the kernel when the kernel is
ready.  A bus write to it never waits.  The same fact removes ``reverse_stream``'s rule-4 hazard: a
one-value register cannot saturate.

**Why a routed producer never stalls the bus.**  It writes only what its credit says fits
(:meth:`CreditStreamMasterIF.write`), and the credit counts every word not yet consumed -- in the
writer, on the bus, and in the queue -- so the queue always has room for what arrives.
:attr:`MemSlaveWStream.nstall <waveflow.hw.mm_queue.MemSlaveWStream.nstall>` counts the
violations, and the gate asserts it stays zero.

**Base addresses.**  Each writer's target is a run-time input (:attr:`MmStreamWriter.target`), set
when the system is placed -- in RTL a port the system top drives -- so neither kernel's RTL depends on
where the other sits (``plans/bus_address_map.md`` D6).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, ClassVar

import numpy as np

from waveflow.hw.clock import Clock
from waveflow.hw.hw_module import HwModule, HwParam
from waveflow.hw.interface import Interface, InterfaceEndpoint, StreamIF, StreamIFMaster, StreamIFSlave
from waveflow.hw.memif import MMIFMaster, MMIFSlave, Words
from waveflow.hw.reverse_stream import (
    CREDIT_DRAIN,
    CTR_BITS,
    CreditStreamMasterIF,
    CreditStreamSlaveIF,
)
from waveflow.simulation.simobj import ProcessGen

#: AXI4's longest INCR burst, in beats.
AXI4_MAX_BEATS = 256


def _dtype(dw: int) -> np.dtype:
    return np.dtype(np.uint32) if dw <= 32 else np.dtype(np.uint64)


# ---------------------------------------------------------------------------
# The credit-in view
# ---------------------------------------------------------------------------

@dataclass
class MemSlaveCreditIn(HwModule):
    """A credit-in window: the bus writes a cumulative count; the kernel receives it as a stream.

    Endpoints: ``s_mem`` (bind to a crossbar slave port, or put behind an adaptor) and ``m_out``
    (:class:`~waveflow.hw.interface.StreamIFMaster`, :attr:`ctr_bits` wide, one word per value) --
    joined to the producer's ``CreditStreamMasterIF.crd_ep``.

    A write anywhere in the window replaces the value (masked to :attr:`ctr_bits`) and never waits.
    The view delivers the **newest** value whenever the kernel's stream has room, so a kernel that
    falls behind sees fewer values, never older ones.  A read returns the current value.
    """

    view_kind: ClassVar[str] = "credit_in"

    mem_dwidth: HwParam[int] = 64
    ctr_bits: HwParam[int] = CTR_BITS
    window: int = 4096
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        dw = int(self.mem_dwidth)
        self.s_mem = MMIFSlave(name=f"{self.name}_s_mem", sim=self.sim, bitwidth=dw,
                               rx_write_proc=self._on_write, rx_read_proc=self._on_read,
                               peek_read=self._peek)
        self.m_out = StreamIFMaster(name=f"{self.name}_m_out", sim=self.sim,
                                    bitwidth=int(self.ctr_bits), has_tlast=True)
        for ep in (self.s_mem, self.m_out):
            self.add_endpoint(ep)
        self._mask = (1 << int(self.ctr_bits)) - 1
        self._dt = _dtype(dw)
        self.value = 0
        self._dirty = False
        self._wake = None
        #: Bus writes taken, and values handed to the kernel (fewer when the kernel lags).
        self.nwrites = 0
        self.ndelivered = 0

    def run_proc(self) -> ProcessGen[None]:
        while True:
            if not self._dirty:
                self._wake = self.env.event()
                yield self._wake
            v, self._dirty = self.value, False
            yield from self.m_out.write(np.asarray([v], dtype=_dtype(int(self.ctr_bits))))
            self.ndelivered += 1

    def _on_write(self, words: Words, local_addr: int) -> ProcessGen[None]:
        for w in np.asarray(words).tolist():
            self.value = int(w) & self._mask
            self.nwrites += 1
        self._dirty = True
        if self._wake is not None and not self._wake.triggered:
            self._wake.succeed()
        yield self.env.timeout(0)

    def _on_read(self, nwords: int, local_addr: int) -> ProcessGen[Words]:
        yield self.env.timeout(0)
        return self._peek(nwords, local_addr)

    def _peek(self, nwords: int, local_addr: int) -> Words:
        return np.full(int(nwords), self.value, dtype=self._dt)


# ---------------------------------------------------------------------------
# The bus writer
# ---------------------------------------------------------------------------

@dataclass
class MmStreamWriter(HwModule):
    """Stream in, bus writes out: the bus-master half of a routed channel.

    ``mode="queue"``: each burst on :attr:`s_in` (a producer's write, ended by TLAST) goes to a
    queue-in view as one packet, ``[len | words]``, in bursts of at most :attr:`max_burst`.
    ``mode="credit"``: each value on :attr:`s_in` goes to a credit-in view as one word; values that
    queued up behind a slow bus are **coalesced** to the newest (cumulative, so nothing is lost) -- a
    slow bus costs staleness, never a backlog.

    :attr:`target` is the view's bus address, a run-time input set when the system is placed.  The
    writer waits for nothing on the bus side: in queue mode the producer's credit already proved the
    room, and a credit-in view never waits.
    """

    mode: str = "queue"
    mem_dwidth: HwParam[int] = 64
    in_bitwidth: int = 64
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))
    max_outstanding: int = 1
    issue_cycles: int = 0

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.mode not in ("queue", "credit"):
            raise ValueError(f"{self.name}: mode must be 'queue' or 'credit', got {self.mode!r}")
        dw = int(self.mem_dwidth)
        self.s_in = StreamIFSlave(name=f"{self.name}_s_in", sim=self.sim,
                                  bitwidth=int(self.in_bitwidth), has_tlast=True)
        self.m_mem = MMIFMaster(name=f"{self.name}_m_mem", sim=self.sim, bitwidth=dw,
                                max_outstanding=self.max_outstanding,
                                issue_cycles=self.issue_cycles)
        for ep in (self.s_in, self.m_mem):
            self.add_endpoint(ep)
        self._dt = _dtype(dw)
        #: The target view's bus address (``None`` until placed).
        self.target: int | None = None
        #: Longest burst the target view takes at one address.
        self.max_burst = AXI4_MAX_BEATS
        #: Bus write transactions issued, and stream items taken (observability).
        self.nwrites = 0
        self.nitems = 0

    def pre_sim(self) -> None:
        super().pre_sim()
        if self.target is None:
            raise RuntimeError(f"{self.name}: no target address -- place the channel "
                               f"(MmCreditStreamIF.place) before running")

    def run_proc(self) -> ProcessGen[None]:
        if self.mode == "queue":
            while True:
                words = np.asarray((yield from self.s_in.get()), dtype=self._dt).reshape(-1)
                self.nitems += 1
                pkt = np.concatenate([np.asarray([len(words)], dtype=self._dt), words])
                for i in range(0, len(pkt), self.max_burst):
                    yield from self.m_mem.write(pkt[i:i + self.max_burst], self.target)
                    self.nwrites += 1
        else:
            while True:
                got = yield from self.s_in.get()
                self.nitems += 1
                for _ in range(CREDIT_DRAIN):        # bounded: take any newer values already queued
                    newer = yield from self.s_in.get_nb()
                    if newer is None:
                        break
                    got = newer
                    self.nitems += 1
                v = int(np.asarray(got).reshape(-1)[-1])
                yield from self.m_mem.write(np.asarray([v], dtype=self._dt), self.target)
                self.nwrites += 1


# ---------------------------------------------------------------------------
# The routed channel
# ---------------------------------------------------------------------------

@dataclass
class MmCreditStreamIF(Interface):
    """A :class:`~waveflow.hw.reverse_stream.CreditStreamIF` whose two directions cross a bus.

    Bind it like a direct one -- ``"master"`` to the producer's ``CreditStreamMasterIF``,
    ``"slave"`` to the consumer's ``CreditStreamSlaveIF`` -- **after** both kernels' devices are
    built (``build_mm_device`` joins the views to the endpoints' other halves).  It builds the two
    writers (:attr:`fwd_writer`, :attr:`crd_writer`); put their ``m_mem`` ports on the crossbar
    (:meth:`bus_masters`), then, once addresses are assigned, :meth:`place` it with the two views'
    map entries.  The consumer should batch its credit (``crd_every``): every offer is a bus write.
    """

    bitwidth: int = 64
    """Forward word width -- also the bus width."""
    ctr_bits: int = CTR_BITS
    clk: Clock | None = None
    writer_depth: int = 2
    """Depth of the credit stream between the consumer and its credit writer."""
    fwd_depth: int = 2
    """Depth, in words, of the FIFO between the producer and its forward writer -- a real FIFO in RTL
    too (the system top instantiates it at this depth).  The writer is store-and-forward (it needs a
    write's length before its words), so while it bursts one write it reads nothing; the FIFO is what
    lets the producer keep going.  Give it at least one write's words, or the producer stalls for
    every burst (measured on examples/markov: 103 cycles per 64-draw chunk at depth 0, against 64)."""
    max_outstanding: int = 1
    issue_cycles: int = 0

    type_name = "mm_credit_stream_if"

    def __post_init__(self) -> None:
        self.endpoint_names = ("master", "slave")
        if self.clk is None:
            raise ValueError(f"{type(self).__name__} needs clk")
        super().__post_init__()
        kw = dict(sim=self.sim, mem_dwidth=int(self.bitwidth), clk=self.clk,
                  max_outstanding=self.max_outstanding, issue_cycles=self.issue_cycles)
        self.fwd_writer = MmStreamWriter(name=f"{self.name}_fwd_wr", mode="queue",
                                         in_bitwidth=int(self.bitwidth), **kw)
        self.crd_writer = MmStreamWriter(name=f"{self.name}_crd_wr", mode="credit",
                                         in_bitwidth=int(self.ctr_bits), **kw)
        #: The receiving queue's depth, from :meth:`place` (``None`` until then).
        self.depth: int | None = None
        self.qin = None
        self.crd_in = None

    def bus_masters(self) -> list[MMIFMaster]:
        """The two bus masters this channel adds to the crossbar: forward, then credit."""
        return [self.fwd_writer.m_mem, self.crd_writer.m_mem]

    def bind(self, ep_name: str, endpoint: InterfaceEndpoint) -> None:
        if ep_name not in ("master", "slave"):
            raise KeyError(f"{self.name}: only 'master' and 'slave' sides, got {ep_name!r}")
        want = CreditStreamMasterIF if ep_name == "master" else CreditStreamSlaveIF
        if not isinstance(endpoint, want):
            raise TypeError(f"{self.name}: '{ep_name}' must be a {want.__name__}")
        if int(endpoint.ctr_bits) != int(self.ctr_bits):
            raise ValueError(f"{self.name}: counter widths differ ({self.ctr_bits} vs "
                             f"{endpoint.ctr_bits} on '{ep_name}')")
        if ep_name == "master":
            if endpoint.crd_ep.interface is None:
                raise RuntimeError(f"{self.name}: {endpoint.name}'s credit half is not joined to a "
                                   f"credit-in view -- build the producer's mm device first")
            self._join(f"{self.name}_fwd", endpoint.fwd_ep, self.fwd_writer.s_in, int(self.bitwidth),
                       int(self.fwd_depth))
        else:
            if endpoint.fwd_ep.interface is None:
                raise RuntimeError(f"{self.name}: {endpoint.name}'s forward half is not joined to a "
                                   f"queue-in view -- build the consumer's mm device first")
            self._join(f"{self.name}_crd", endpoint.crd_ep, self.crd_writer.s_in, int(self.ctr_bits),
                       int(self.writer_depth))
        super().bind(ep_name, endpoint)

    def _join(self, name: str, master, slave, bw: int, depth: int) -> None:
        si = StreamIF(name=name, sim=self.sim, clk=self.clk, bitwidth=bw, depth=int(depth))
        si.bind(ep_name="master", endpoint=master)
        si.bind(ep_name="slave", endpoint=slave)

    def place(self, qin, crd_in) -> None:
        """Point the writers at the two views: *qin* is the consumer's queue-in entry, *crd_in* the
        producer's credit-in entry (``ViewEntry``s from a placed ``MemSlaveMap``)."""
        if qin.kind != "queue_in" or crd_in.kind != "credit_in":
            raise ValueError(f"{self.name}: place(qin=a queue_in view, crd_in=a credit_in view), got "
                             f"{qin.kind!r} and {crd_in.kind!r}")
        if int(qin.mem_dwidth) != int(self.bitwidth):
            raise ValueError(f"{self.name}: queue '{qin.name}' is {qin.mem_dwidth} bits, the channel "
                             f"{self.bitwidth}")
        self.qin, self.crd_in = qin, crd_in
        self.depth = int(qin.depth)
        m = self.endpoints.get("master")
        if m is not None and self.depth <= int(m.resp_words):
            raise ValueError(f"{self.name}: depth {self.depth} leaves no room once "
                             f"resp_words={m.resp_words} is reserved")
        self.fwd_writer.target = int(qin.base)
        self.fwd_writer.max_burst = int(qin.max_burst)
        #: The longest packet the forward writer carries -- the receiving queue's depth, which is
        #: what its csynth'd top is specialized on (``mm_writer_gen.writer_top_name``).
        self.fwd_writer.max_packet = self.depth
        self.crd_writer.target = int(crd_in.base)


__all__ = ["MemSlaveCreditIn", "MmStreamWriter", "MmCreditStreamIF"]
