"""
memif.py: Memory-mapped (MM) interface endpoints and interconnects.

Generic endpoints
-----------------
``MMIFMaster`` and ``MMIFSlave`` are endpoint types that are independent of
the underlying interconnect.  A component that reads or writes memory always
uses these endpoints; the choice of interconnect (crossbar, direct) is made
when wiring the design.

Convenience methods on ``MMIFMaster``
--------------------------------------
read_schema(schema_type, addr, word_bw=32)
    Read and deserialize one schema instance.
write_schema(obj, addr, word_bw=32)
    Serialize and write one schema instance.
read_array(element_type, count, addr, word_bw=32)
    Read and deserialize *count* elements (returns ``np.ndarray`` for scalar
    ``FloatField``/``IntField`` types, ``list`` otherwise).
write_array(elements, element_type, addr, word_bw=32)
    Serialize and write an array of elements (accepts ``np.ndarray`` fast path
    for scalar types or an iterable of schema instances / raw values).

Crossbar interconnect — AXIMMCrossBarIF
-----------------------------------------
Multi-master × multi-slave, address-based routing.  FULL (burst) and LITE
(register-style, single-word) protocols are configured per slave *at bind
time* rather than on the slave endpoint itself::

    xbar.bind("slave_0", mem_ep)                             # default: FULL
    xbar.bind("slave_1", reg_ep, protocol=AXIMMProtocol.LITE)

Latency model
^^^^^^^^^^^^^
FULL write : (latency_init + nwords) / clk.freq  seconds total
FULL read  : latency_init/clk.freq (request wire)
           + slave rx_read_proc duration
           + (latency_read_return + nwords) / clk.freq (return wire)
LITE write : nwords × latency_per_word / clk.freq  (one txn per word)
LITE read  : nwords × latency_per_word / clk.freq  (one txn per word)

Direct interconnect — DirectMMIF
----------------------------------
Point-to-point, one master to one slave.  The master's address is passed
directly to the slave's callback (no address translation)::

    direct = DirectMMIF(sim=sim, clk=clk)
    direct.bind("master", master_ep)
    direct.bind("slave",  slave_ep)

Address assignment utility
--------------------------
assign_address_ranges(slaves, [(base_addr, size), ...]) -> list[AXIMMAddressRange]
"""
from __future__ import annotations

import warnings
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, ClassVar

import numpy as np
import simpy

from waveflow.hw.interface import InterfaceEndpoint, QueuedTransferIF, Words
from waveflow.hw.hwstmt import MMArrayReadStmt, MMArrayWriteStmt, SynthCallStmt
from waveflow.hw.synth import synthesizable
from waveflow.simulation.simobj import ProcessGen


class AXIMMProtocol(Enum):
    """AXI-MM protocol variant for a crossbar slave port."""
    FULL = "full"   # burst-capable; amortised latency over nwords
    LITE = "lite"   # register-style; one transaction per word


@dataclass(frozen=True)
class AXIMMAddressRange:
    """Half-open byte-address range [base_addr, base_addr + size)."""

    base_addr: int
    size: int

    def contains(self, addr: int) -> bool:
        return self.base_addr <= addr < self.base_addr + self.size

    def to_local(self, addr: int) -> int:
        return addr - self.base_addr


# Callable type aliases for slave callbacks.
RxWriteProc = Callable[[Words, int], ProcessGen[None]]   # (words, local_addr) -> ProcessGen[None]
RxReadProc  = Callable[[int,   int], ProcessGen[Words]]  # (nwords, local_addr) -> ProcessGen[Words]
PeekRead    = Callable[[int,   int], Words]              # (nwords, local_addr) -> Words (untimed)


# ---------------------------------------------------------------------------
# Poll conditions — the restricted, lowerable predicate for poll_until
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PollCond:
    """A small declarative poll predicate usable by both sim and (later) synth.

    The op is one of ``==`` / ``!=`` (use the :class:`Eq` / :class:`Ne`
    subclasses); ``rhs`` is the value the polled word is compared against — a
    literal **or** a value captured from a prior runtime read (in simulation
    both are plain integers).

    :meth:`eval` is the sim form (``cond.eval(read_value)``); the *same* object
    is the single source of truth a future extractor lowers to the C++ poll loop
    (decision D4 in ``plans/poll_until_lt_model.md``) — no sim/synth drift.  Sim
    expressiveness is intentionally limited to ``==`` / ``!=`` vs a single word,
    which is exactly what real polls are (``tail != head``).
    """

    rhs: Any
    op: ClassVar[str] = ""

    def eval(self, value: int) -> bool:
        """True when ``value <op> rhs`` (the sim evaluation of this poll)."""
        v, r = int(value), int(self.rhs)
        if self.op == "==":
            return v == r
        if self.op == "!=":
            return v != r
        raise ValueError(f"PollCond: unsupported op {self.op!r}")


@dataclass(frozen=True)
class Eq(PollCond):
    """Poll predicate ``read(addr) == rhs``."""

    op: ClassVar[str] = "=="


@dataclass(frozen=True)
class Ne(PollCond):
    """Poll predicate ``read(addr) != rhs`` (e.g. the ring dequeue ``tail != head``)."""

    op: ClassVar[str] = "!="


# ---------------------------------------------------------------------------
# Synthesizable lowering of poll_until (step 5 — the C++ ring-poll twin)
# ---------------------------------------------------------------------------

@dataclass
class LoweredPollCond:
    """The lowered, synthesizable form of a :class:`PollCond` — what the
    extractor resolves an ``Eq(x)`` / ``Ne(x)`` call-site argument to.

    ``op`` is ``'=='`` / ``'!='``; ``rhs`` is the comparison value, either a
    compile-time constant or a runtime ``HwVar`` already in scope (the ring poll
    compares ``tail != head`` where ``head`` is a prior read).  It carries none
    of the loosely-timed AT-model data (``poll_beat_cost`` / ``discovery``):
    a hardware poll just spins on the word."""

    op: str
    rhs: Any   # HwVar | int (constant)


class PollUntilStmt(SynthCallStmt):
    """IR node for ``val = master.poll_until(addr, cond, poll_interval)`` — a
    blocking poll over the master's m_axi pointer, lowered to the reusable C++
    ring-poll primitive (``poll_until_impl::poll_until_{eq,ne}``).

    ``inputs[0]`` is the watched byte address; ``inputs[1]`` is the
    :class:`LoweredPollCond`; ``inputs[2]`` (if present) is ``poll_interval`` —
    sim-only (AT-model) and **never emitted**.  ``outputs[0]`` is the
    satisfying-value local.  ``poll_beat_cost`` / ``discovery`` are sim-only and
    never reach this IR."""

    @property
    def port(self):
        """The bound ``MMIFMaster`` endpoint (``method.__self__``)."""
        return getattr(self.method, '__self__', None)

    @property
    def addr_expr(self):
        return self.inputs[0] if self.inputs else None

    @property
    def cond(self) -> "LoweredPollCond | None":
        return self.inputs[1] if len(self.inputs) > 1 else None

    def __repr__(self) -> str:
        out = self.outputs[0].name if self.outputs else '?'
        return f"PollUntilStmt({out} = poll_until)"


# ---------------------------------------------------------------------------
# Generic MM slave endpoint
# ---------------------------------------------------------------------------

@dataclass
class MMIFSlave(InterfaceEndpoint):
    """
    Slave (target) endpoint for any MM interconnect.

    Parameters
    ----------
    bitwidth : int
        Data word width in bits.
    rx_write_proc : RxWriteProc | None
        Called as ``rx_write_proc(words, local_addr)`` for each incoming write.
        For LITE crossbar slaves this is called once per word.
    rx_read_proc : RxReadProc | None
        Called as ``rx_read_proc(nwords, local_addr)``; must return a
        ``(nwords,)`` numpy array.
    latency_per_word : float
        Used by ``AXIMMCrossBarIF`` when this slave is bound with
        ``protocol=AXIMMProtocol.LITE``.  Ignored for FULL and DirectMMIF.
    """

    bitwidth: int = 32
    rx_write_proc: RxWriteProc | None = None
    rx_read_proc:  RxReadProc  | None = None
    latency_per_word: float = 2.0
    peek_read: PeekRead | None = None
    """Optional *untimed*, synchronous read ``(nwords, local_addr) -> Words`` of
    the backing store — the snapshot :meth:`MMIFMaster.poll_until` peeks while
    waiting (its bus cost is modeled aggregately, so the peek itself is free).
    :class:`~waveflow.hw.memory.MemComponent` populates it; a slave without one
    cannot be polled."""

    addr_range: AXIMMAddressRange | None = field(init=False)
    bus: simpy.Resource = field(init=False)

    type_name = 'mmif_slave'

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)

    def __post_init__(self) -> None:
        super().__post_init__()
        self.addr_range = None
        self.bus = simpy.Resource(self.env, capacity=1)


# ---------------------------------------------------------------------------
# Generic MM master endpoint
# ---------------------------------------------------------------------------

@dataclass
class MMIFMaster(InterfaceEndpoint):
    """
    Master (initiator) endpoint for any MM interconnect.

    Exposes :meth:`write` and :meth:`read` using global addresses, plus
    convenience methods for schema-typed access.
    """

    bitwidth: int = 32
    master_port: int = field(init=False)

    type_name = 'mmif_master'

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)

    def __post_init__(self) -> None:
        super().__post_init__()
        self.master_port = -1

    def _check_bound(self) -> None:
        if self.interface is None:
            raise RuntimeError(
                f"Cannot transact: {type(self).__name__} is not bound to an interface"
            )

    # ------------------------------------------------------------------
    # Raw word transfers
    # ------------------------------------------------------------------

    def write(self, words: Words, global_addr: int,
              tstart: float | None = None) -> ProcessGen[None]:
        """Write a burst of words to *global_addr*.

        *tstart* (optional) models an **early-anchored pipelined** write: the burst
        is treated as having started at absolute time *tstart*, so it completes at
        ``tstart + nwords*period`` (the wait shortens if *tstart* is in the past).
        ``None`` is the ordinary blocking write."""
        self._check_bound()
        yield self.process(
            self.interface.write(words, global_addr, self.master_port, tstart=tstart))

    def read(self, nwords: int, global_addr: int) -> ProcessGen[Words]:
        """Read *nwords* from *global_addr* and return the word array."""
        self._check_bound()
        proc = self.process(self.interface.read(nwords, global_addr, self.master_port))
        yield proc
        return proc.value

    # ------------------------------------------------------------------
    # Polling (the LT polling-overhead model)
    # ------------------------------------------------------------------

    @synthesizable(stmt_class=PollUntilStmt)
    def poll_until(
        self,
        global_addr: int,
        cond: PollCond,
        poll_interval: float,
        *,
        poll_beat_cost: int = 1,
        discovery: str = "mean",
    ) -> ProcessGen[int]:
        """Block until *cond* holds on a poll of *global_addr*; return the
        satisfying word value.

        The loosely-timed polling-overhead model (see
        ``plans/poll_until_lt_model.md``).  Two costs are modeled separately:

        * **Bandwidth steal** — for the duration of the wait the master registers
          as an *active poller* on the bus/slave it polls, contributing
          ``poll_beat_cost / poll_interval`` to that bus's occupancy ``ov``; the
          interconnect derates **other** masters' real per-word transfers by
          ``1/(1-ov)``.  The poll reads themselves cost no bus time (their cost
          *is* that ``ov`` contribution), so the sim never issues a timed read
          every ``poll_interval`` — it waits, in O(transactions), on the bus's
          write-notify, peeking the watched word untimed.
        * **Discovery latency** — on *cond* true the deterministic mean
          event-to-next-poll gap ``(poll_interval-1)/2`` cycles is added
          (decision D1; a seeded-stochastic mode is deferred).

        ``poll_interval`` is in **cycles**.  ``poll_beat_cost`` (default 1, D3) is
        the per-poll bus beat-occupancy charged.  ``discovery='mean'`` is the
        only mode in v1.
        """
        self._check_bound()
        if discovery != "mean":
            raise ValueError(
                f"poll_until: discovery={discovery!r} unsupported; only 'mean' in v1 (D1)"
            )
        iface = self.interface
        if not hasattr(iface, "register_poller"):
            raise TypeError(
                f"poll_until requires a poll-aware interconnect; "
                f"{type(iface).__name__} does not support active pollers"
            )
        iface.register_poller(global_addr, poll_beat_cost, poll_interval)
        try:
            while True:
                word = iface.poll_peek(global_addr, 1)
                value = int(np.asarray(word).reshape(-1)[0])
                if cond.eval(value):
                    break
                # O(transactions) wait: wake on the next write to this slave and
                # re-check, rather than stepping every poll_interval.
                yield iface.poll_wait_event(global_addr)
            disc_cycles = (float(poll_interval) - 1.0) / 2.0
            if disc_cycles > 0:
                yield self.timeout(disc_cycles / iface.clk.freq)
        finally:
            iface.unregister_poller(global_addr, poll_beat_cost, poll_interval)
        return value

    # ------------------------------------------------------------------
    # Schema convenience methods
    # ------------------------------------------------------------------

    def write_schema(self, obj: Any, addr: int, word_bw: int = 32) -> ProcessGen[None]:
        """Serialize *obj* and write it to *addr*."""
        yield from self.write(obj.serialize(word_bw=word_bw), addr)

    def read_schema(self, schema_type: type, addr: int, word_bw: int = 32) -> ProcessGen[Any]:
        """Read and deserialize one *schema_type* instance from *addr*."""
        nwords = schema_type.nwords_per_inst(word_bw)
        words = yield from self.read(nwords, addr)
        return schema_type().deserialize(words, word_bw=word_bw)

    @synthesizable(stmt_class=MMArrayWriteStmt)
    def write_array(
        self,
        elements: Any,
        element_type: type,
        addr: int,
        count: int | None = None,
        max_count: int | None = None,
        word_bw: int = 32,
    ) -> ProcessGen[None]:
        """Serialize *elements* and write them to *addr*.

        *max_count* is a codegen-only compile-time bound: it sizes the static
        local buffer the generated kernel writes from (``static <ctype>
        buf[max_count]``).  Ignored in simulation; required for an
        m_axi buffered read at codegen time — the generated kernel fails loudly
        without it (each buffer declares its own bound; there is no fallback).

        Accepts a ``np.ndarray`` (the vectorized fast path for any element type
        that enables it via :meth:`DataSchema.to_words_numpy`) or a
        :class:`~waveflow.hw.dataschema.DataArray` / array-like.  Packing is the
        canonical :meth:`DataSchema.serialize` layout — there is no element-type
        switch here; the element type decides whether it has a numpy fast path.

        *count* — when given — selects the first *count* elements before
        packing.  It exists so a synthesizable m_axi write carries an explicit
        runtime length (the static local buffer is sized at ``max``, written in
        ``[0, count)``); the dual of :meth:`read_array`'s *count*.
        """
        words = self._pack_array_words(elements, element_type, word_bw, count=count)
        yield from self.write(words, addr)

    @staticmethod
    def _pack_array_words(
        elements: Any, element_type: type, word_bw: int, count: int | None = None,
    ) -> Words:
        """Pack *elements* into hardware words: the element type's vectorized
        :meth:`DataSchema.to_words_numpy` fast path, else the canonical recursive
        serializer.  Shared by :meth:`write_array` and :meth:`write_array_pipelined`."""
        if count is not None:
            elements = elements[:int(count)]
        words = element_type.to_words_numpy(elements, word_bw)   # vectorized fast path
        if words is None:                                        # canonical recursive pack
            from waveflow.hw.arrayutils import write_array as _pack_array
            words = _pack_array(elements, elem_type=element_type, word_bw=word_bw)
        return words

    @synthesizable(stmt_class=MMArrayReadStmt)
    def read_array(
        self,
        element_type: type,
        count: int,
        addr: int,
        max_count: int | None = None,
        word_bw: int = 32,
    ) -> ProcessGen[np.ndarray | list]:
        """Read and deserialize *count* elements from *addr*.

        *max_count* is a codegen-only compile-time bound: it sizes the static
        local buffer the generated kernel reads into (``static <ctype>
        buf[max_count]``).  Ignored in simulation; required for an
        m_axi buffered read at codegen time — the generated kernel fails loudly
        without it (each buffer declares its own bound; there is no fallback).

        Returns a ``np.ndarray`` for every element type: the element's native
        dtype for scalar ``FloatField`` / ``IntField`` types, and the structured
        ``[('re', …), ('im', …)]`` array for ``ComplexField`` (via the canonical
        :meth:`DataSchema.deserialize`).  No element-type switch lives here — the
        element type supplies its own vectorized path via
        :meth:`DataSchema.from_words_numpy`, falling back to the recursive
        deserializer otherwise.
        """
        nwpe = element_type.nwords_per_inst(word_bw)
        words = yield from self.read(nwpe * count, addr)
        out = element_type.from_words_numpy(words, count, word_bw)   # vectorized fast path
        if out is None:                                             # canonical recursive unpack
            from waveflow.hw.arrayutils import read_array as _unpack_array
            out = _unpack_array(words, element_type, word_bw, shape=count).val
        return out

    # ------------------------------------------------------------------
    # Timed (AT) array transfers — sim-only timing capture
    # ------------------------------------------------------------------
    # Same data path as read_array / write_array, but also report the SimPy times that
    # bracket the bus transfer, so a loosely-timed component can record the transaction
    # (or pad to a calibrated schedule) without hand-bracketing env.now.  These are
    # sim-only (no stmt_class) — the timing is an AT-model concern, not synthesizable —
    # mirroring the stream get_pipelined / write_pipelined helpers.

    def read_array_pipelined(
        self,
        element_type: type,
        count: int,
        addr: int,
        max_count: int | None = None,
        word_bw: int = 32,
    ) -> ProcessGen[tuple[np.ndarray, float, float, int]]:
        """Like :meth:`read_array`, returning ``(data, tstart, tend, nwords)`` where
        ``tstart`` / ``tend`` are the SimPy times bracketing the bus read and ``nwords`` is
        the number of words transferred."""
        nwords = element_type.nwords_per_inst(word_bw) * count
        tstart = self.env.now
        data = yield from self.read_array(
            element_type, count, addr, max_count=max_count, word_bw=word_bw)
        tend = self.env.now
        return data, tstart, tend, nwords

    def read_array_anchored(
        self,
        element_type: type,
        count: int,
        addr: int,
        max_count: int | None = None,
        word_bw: int = 32,
    ) -> ProcessGen[tuple[np.ndarray, float]]:
        """Blocking read that returns ``(data, tstart)`` with *tstart* **back-calculated**
        from the completion time (the time the first word arrived), mirroring
        :meth:`StreamIFSlave.get_pipelined`:

            tstart = now_after - (nwords - 1) * clk.period

        The read still blocks for the whole burst (the consumer needs all of it); *tstart*
        is the early pipeline-fill anchor for the next stage's overlap accounting."""
        nwords = element_type.nwords_per_inst(word_bw) * count
        data = yield from self.read_array(
            element_type, count, addr, max_count=max_count, word_bw=word_bw)
        period = 1.0 / float(self.interface.clk.freq)
        tstart = self.env.now - max(0, nwords - 1) * period
        return data, tstart

    def write_array_pipelined(
        self,
        elements: Any,
        element_type: type,
        addr: int,
        count: int | None = None,
        max_count: int | None = None,
        word_bw: int = 32,
        t_out_start: float | None = None,
    ) -> ProcessGen[tuple[float, float, int]]:
        """Like :meth:`write_array`, returning ``(tstart, tend, nwords)`` where ``tstart`` /
        ``tend`` bracket the bus write and ``nwords`` is the number of words transferred.

        With *t_out_start* the write is **early-anchored** (pipelined): the burst is modeled
        as having started at absolute time *t_out_start*, so it completes at
        ``t_out_start + nwords*period`` (the wait shortens if the anchor is in the past) —
        this is what lets a producer's write overlap an earlier consumer's read.
        ``t_out_start=None`` preserves the ordinary blocking write (backward-compatible)."""
        words = self._pack_array_words(elements, element_type, word_bw, count=count)
        nwords = int(np.asarray(words).shape[0])
        tstart = self.env.now
        yield from self.write(words, addr, tstart=t_out_start)
        tend = self.env.now
        return tstart, tend, nwords

    # ------------------------------------------------------------------
    # Element-coordinate region view (the sim twin of the C++ read_array_slice)
    # ------------------------------------------------------------------

    def _word_bytes(self, word_bw: int) -> int:
        """Address units spanned by one packed word — the bound interface's addressing
        convention (byte-addressed AXI: ``word_bw // 8``; word-addressed: ``1``).  The single
        place the element→address conversion consults, so accelerators never hard-code
        ``mem_bw // 8`` (the ``_byte_addr`` antipattern)."""
        iface = self.interface
        if iface is not None and not getattr(iface, "byte_addressable", True):
            return 1
        return word_bw // 8

    def region(self, base_addr: int, element_type: type, *, word_bw: int = 32) -> "Region":
        """A memory-region view bound to a byte *base_addr* and an *element_type*, addressed by
        **element index**.

        The SimPy twin of the C++ ``read_array_slice`` / ``read_array_lane`` element-coordinate
        contract (and the PynQ-``allocate`` analogue): ``region.read_slice(i0, i1)`` moves elements
        ``[i0, i1)`` and the interface does the element→byte conversion (see :meth:`_word_bytes`).
        The base is a byte address (host/allocator-owned, width-agnostic); indices within are
        element coordinates (width-agnostic, HLS-natural)."""
        return Region(
            master=self, base_addr=int(base_addr),
            element_type=element_type, word_bw=int(word_bw),
        )


#: A per-transfer instrumentation hook ``(rw, i0, nwords, tstart, tend) -> None`` — fired by
#: :meth:`Region.read_slice` / :meth:`Region.write_slice` after each transfer so a loosely-timed
#: component can record it (the timeline) without the data path ever unpacking timing.  ``rw`` is
#: ``"read"`` / ``"write"``; ``i0`` is the element start index.
OnTransfer = Callable[[str, int, int, float, float], None]


@dataclass
class Region:
    """Element-indexed view of a memory region for an :class:`MMIFMaster` (built via
    :meth:`MMIFMaster.region`).

    Binds a byte ``base_addr`` + ``element_type``; **all indices are element coordinates**.
    :meth:`read_slice` / :meth:`write_slice` mirror the C++ ``read_array_slice`` /
    ``write_array_slice`` contract so the SimPy model and the generated kernel address memory
    identically — and they return just data / nothing, exactly like the hardware call.  Timing is
    *not* on the data path: set :attr:`on_transfer` and each slice reports ``(rw, i0, nwords,
    tstart, tend)`` to it (the loosely-timed instrumentation hook), so the element→byte conversion
    *and* the AT timing capture both live in the framework, not hand-rolled on each accelerator.
    """

    master: MMIFMaster
    base_addr: int
    element_type: type
    word_bw: int = 32
    on_transfer: OnTransfer | None = None

    @property
    def _elem_bytes(self) -> int:
        return (self.element_type.nwords_per_inst(self.word_bw)
                * self.master._word_bytes(self.word_bw))

    def byte_of(self, i: int) -> int:
        """Byte address of element *i* (the element-coordinate → address conversion)."""
        return self.base_addr + int(i) * self._elem_bytes

    def read_slice(self, i0: int, i1: int) -> ProcessGen[Any]:
        """Read elements ``[i0, i1)`` (element coordinates) and return the deserialized array —
        the sim twin of ``read_array_slice<W>(mem, i0, i1, x)``.  Fires :attr:`on_transfer`."""
        data, t0, t1, nw = yield from self.master.read_array_pipelined(
            self.element_type, int(i1) - int(i0), self.byte_of(i0), word_bw=self.word_bw)
        if self.on_transfer is not None:
            self.on_transfer("read", int(i0), nw, t0, t1)
        return data

    def write_slice(
        self, i0: int, elements: Any, element_type: type | None = None,
    ) -> ProcessGen[None]:
        """Write *elements* starting at element index *i0* — the sim twin of
        ``write_array_slice``.  *element_type* defaults to the region's (pass it when the written
        elements differ, e.g. an output format whose addressing stride matches).  Fires
        :attr:`on_transfer`."""
        t0, t1, nw = yield from self.master.write_array_pipelined(
            elements, element_type or self.element_type, self.byte_of(i0), word_bw=self.word_bw)
        if self.on_transfer is not None:
            self.on_transfer("write", int(i0), nw, t0, t1)

    def read_slice_pipelined(self, i0: int, i1: int) -> ProcessGen[tuple[Any, float]]:
        """Like :meth:`read_slice`, returning ``(data, tstart)`` where *tstart* is the
        back-calculated time the first element arrived (the early pipeline-fill anchor) —
        the memory/Region mirror of :meth:`StreamIFSlave.get_pipelined`.  Fires
        :attr:`on_transfer`."""
        nwords = self.element_type.nwords_per_inst(self.word_bw) * (int(i1) - int(i0))
        data, tstart = yield from self.master.read_array_anchored(
            self.element_type, int(i1) - int(i0), self.byte_of(i0), word_bw=self.word_bw)
        if self.on_transfer is not None:
            self.on_transfer("read", int(i0), nwords, tstart, self.master.env.now)
        return data, tstart

    def write_slice_pipelined(
        self, i0: int, elements: Any, t_out_start: float, element_type: type | None = None,
    ) -> ProcessGen[None]:
        """Like :meth:`write_slice`, but **early-anchored** at absolute time *t_out_start*:
        the burst is modeled as having started then, so it completes at
        ``t_out_start + nwords*period`` (overlapping an earlier read).  The memory/Region
        mirror of :meth:`StreamIFMaster.write_pipelined`.  Fires :attr:`on_transfer`."""
        t0, t1, nw = yield from self.master.write_array_pipelined(
            elements, element_type or self.element_type, self.byte_of(i0),
            word_bw=self.word_bw, t_out_start=t_out_start)
        if self.on_transfer is not None:
            self.on_transfer("write", int(i0), nw, t0, t1)


# ---------------------------------------------------------------------------
# Shared poll support for poll-aware MM interconnects
# ---------------------------------------------------------------------------

class _MMPollSupport:
    """Active-poller registry + write-notify + occupancy derating shared by the
    poll-aware MM interconnects (:class:`AXIMMCrossBarIF`, :class:`DirectMMIF`).

    The interconnect owns this because it has the global, per-bus topology view
    the model needs: ``ov`` (the poll bandwidth-steal fraction) is summed **per
    slave/bus** (keyed by ``ep_name``) so independent ports never derate each
    other, and it is evaluated at each transaction's time because the
    active-poller set is finite and time-varying.  See
    ``plans/poll_until_lt_model.md``.

    Subclasses provide :meth:`_poll_ep_for_addr` (address → bus key + slave +
    local addr), call :meth:`_init_poll_support` in ``__post_init__``, and call
    :meth:`_notify_write` after each completed write.
    """

    #: ``ov`` is clamped just under 1 so ``1/(1-ov)`` cannot diverge on a
    #: polling-bound config (e.g. a 1-cycle poll is ``ov=1``); the per-word
    #: stretch saturates at ~100x and one warning fires per bus per saturation
    #: onset (decision D2).
    POLL_OV_CEILING: ClassVar[float] = 0.99

    def _init_poll_support(self) -> None:
        self._pollers: dict[str, list[tuple[float, float]]] = {}
        self._poll_write_events: dict[str, simpy.Event] = {}
        self._poll_saturated: dict[str, bool] = {}

    # -- subclass hook ------------------------------------------------------
    def _poll_ep_for_addr(self, global_addr: int) -> tuple[str, MMIFSlave, int]:
        """Return ``(bus_key, slave_ep, local_addr)`` for *global_addr*."""
        raise NotImplementedError

    # -- write-notify (the O(transactions) wakeup for blocked pollers) ------
    def _poll_event(self, ep_name: str) -> simpy.Event:
        ev = self._poll_write_events.get(ep_name)
        if ev is None:
            ev = self.env.event()
            self._poll_write_events[ep_name] = ev
        return ev

    def _notify_write(self, ep_name: str) -> None:
        """Fire (and renew) the per-slave write-notify so blocked pollers
        re-check.  Called after a write lands so a poller's untimed peek already
        sees the new value (no lost wakeup)."""
        ev = self._poll_write_events.get(ep_name)
        if ev is not None and not ev.triggered:
            ev.succeed()
        self._poll_write_events[ep_name] = self.env.event()

    # -- active-poller registry (used by MMIFMaster.poll_until) -------------
    def register_poller(
        self, global_addr: int, poll_beat_cost: float, poll_interval: float
    ) -> None:
        ep_name, _, _ = self._poll_ep_for_addr(global_addr)
        self._pollers.setdefault(ep_name, []).append(
            (float(poll_beat_cost), float(poll_interval))
        )

    def unregister_poller(
        self, global_addr: int, poll_beat_cost: float, poll_interval: float
    ) -> None:
        ep_name, _, _ = self._poll_ep_for_addr(global_addr)
        lst = self._pollers.get(ep_name)
        if lst:
            try:
                lst.remove((float(poll_beat_cost), float(poll_interval)))
            except ValueError:
                pass

    def poll_peek(self, global_addr: int, nwords: int) -> Words:
        """Untimed snapshot read of *nwords* at *global_addr* (zero sim time)."""
        ep_name, slave_ep, local_addr = self._poll_ep_for_addr(global_addr)
        if getattr(slave_ep, "peek_read", None) is None:
            raise RuntimeError(
                f"poll_until: slave '{getattr(slave_ep, 'name', ep_name)}' exposes "
                f"no peek_read (an untimed synchronous read); it cannot be polled"
            )
        return slave_ep.peek_read(int(nwords), int(local_addr))

    def poll_wait_event(self, global_addr: int) -> simpy.Event:
        ep_name, _, _ = self._poll_ep_for_addr(global_addr)
        return self._poll_event(ep_name)

    # -- occupancy derating -------------------------------------------------
    def _poll_occupancy(self, ep_name: str) -> float:
        """``ov = Σ poll_beat_cost_i / poll_interval[i]`` over the pollers active
        on *ep_name*, clamped to :attr:`POLL_OV_CEILING` (warn once per onset)."""
        lst = self._pollers.get(ep_name)
        if not lst:
            self._poll_saturated[ep_name] = False
            return 0.0
        ov = sum(cost / interval for cost, interval in lst if interval > 0)
        if ov >= self.POLL_OV_CEILING:
            if not self._poll_saturated.get(ep_name):
                warnings.warn(
                    f"{type(self).__name__} '{getattr(self, 'name', '?')}': bus "
                    f"'{ep_name}' is polling-bound (ov={ov:.3f} >= 1); clamping to "
                    f"{self.POLL_OV_CEILING} (per-word occupancy stretched "
                    f"~{1.0 / (1.0 - self.POLL_OV_CEILING):.0f}x). A poll interval "
                    f"this aggressive saturates the bus merely checking the condition.",
                    stacklevel=2,
                )
                self._poll_saturated[ep_name] = True
            return self.POLL_OV_CEILING
        self._poll_saturated[ep_name] = False
        return ov

    def _poll_stretch(self, ep_name: str) -> float:
        """Per-word occupancy derating factor ``1/(1-ov)`` for *ep_name* (the
        ``nwords`` term is multiplied by this; init/address latency is not)."""
        return 1.0 / (1.0 - self._poll_occupancy(ep_name))


# ---------------------------------------------------------------------------
# AXI-MM Crossbar interconnect
# ---------------------------------------------------------------------------

@dataclass
class AXIMMCrossBarIF(_MMPollSupport, QueuedTransferIF):
    """
    AXI-MM crossbar connecting ``nports_master`` master ports to
    ``nports_slave`` slave ports.

    Routing is address-based; the crossbar decodes ``global_addr`` against each
    slave's :attr:`~MMIFSlave.addr_range` and computes
    ``local_addr = global_addr - slave.addr_range.base_addr`` before invoking
    the slave callbacks.

    The protocol (FULL vs LITE) for each slave is set at bind time::

        xbar.bind("slave_0", mem_ep)                              # FULL (default)
        xbar.bind("slave_1", reg_ep, protocol=AXIMMProtocol.LITE)

    Endpoint names
    --------------
    ``'master_0'`` … ``'master_{nports_master-1}'`` — bind :class:`MMIFMaster`

    ``'slave_0'`` … ``'slave_{nports_slave-1}'`` — bind :class:`MMIFSlave`

    Parameters
    ----------
    nports_master : int
        Number of master (initiator) ports.
    nports_slave : int
        Number of slave (target) ports.
    latency_init : float
        Fixed wire-latency cycles added to every FULL forward transfer.
    latency_read_return : float
        Fixed wire-latency cycles added to every FULL read return transfer.
    byte_addressable : bool
        When ``True`` (default, AXI convention) addresses are byte addresses
        and each 32-bit word spans 4 bytes.  When ``False`` (word-addressed)
        each word spans 1 address unit.
    clk : Clock
        Reference clock; all cycle counts are divided by ``clk.freq``.
    bitwidth : int | None
        Data word width in bits; inferred from the first bound endpoint if None.
    """

    nports_master: int = 1
    nports_slave: int = 1
    latency_read_return: float = 0.
    byte_addressable: bool = True

    type_name = 'aximm_crossbar_if'

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)

    def __post_init__(self) -> None:
        if self.nports_master < 1:
            raise ValueError("nports_master must be at least 1")
        if self.nports_slave < 1:
            raise ValueError("nports_slave must be at least 1")
        self.endpoint_names = tuple(
            [f'master_{i}' for i in range(self.nports_master)] +
            [f'slave_{j}'  for j in range(self.nports_slave)]
        )
        self._slave_protocols: dict[str, AXIMMProtocol] = {}
        super().__post_init__()
        self._init_poll_support()

    # ------------------------------------------------------------------
    # Bind
    # ------------------------------------------------------------------

    def bind(
        self,
        ep_name: str,
        endpoint: InterfaceEndpoint,
        protocol: AXIMMProtocol = AXIMMProtocol.FULL,
    ) -> None:
        """Bind *endpoint* to *ep_name*.

        For slave ports the *protocol* keyword selects FULL (burst, default)
        or LITE (single-word-per-transaction) behaviour.
        """
        if ep_name.startswith('master_'):
            try:
                idx = int(ep_name[7:])
            except ValueError:
                raise KeyError(f"Invalid endpoint name '{ep_name}' for AXIMMCrossBarIF")
            if idx >= self.nports_master:
                raise KeyError(
                    f"Master port {idx} is out of range for crossbar with "
                    f"{self.nports_master} master port(s)"
                )
            if not isinstance(endpoint, MMIFMaster):
                raise TypeError(
                    "Master sides of AXIMMCrossBarIF must bind to MMIFMaster"
                )
            self._validate_and_set_bitwidth(endpoint)
            super().bind(ep_name, endpoint)
            endpoint.master_port = idx

        elif ep_name.startswith('slave_'):
            try:
                idx = int(ep_name[6:])
            except ValueError:
                raise KeyError(f"Invalid endpoint name '{ep_name}' for AXIMMCrossBarIF")
            if idx >= self.nports_slave:
                raise KeyError(
                    f"Slave port {idx} is out of range for crossbar with "
                    f"{self.nports_slave} slave port(s)"
                )
            if not isinstance(endpoint, MMIFSlave):
                raise TypeError(
                    "Slave sides of AXIMMCrossBarIF must bind to MMIFSlave"
                )
            self._validate_and_set_bitwidth(endpoint)
            super().bind(ep_name, endpoint)
            self._slave_protocols[ep_name] = protocol

        else:
            raise KeyError(f"Invalid endpoint name '{ep_name}' for AXIMMCrossBarIF")

    # ------------------------------------------------------------------
    # Address decode
    # ------------------------------------------------------------------

    def _decode_address(self, global_addr: int) -> tuple[str, MMIFSlave, int]:
        """Return (ep_name, slave_ep, local_addr) for *global_addr*."""
        for ep_name, ep in self.endpoints.items():
            if ep_name.startswith('slave_') and ep is not None:
                if ep.addr_range is not None and ep.addr_range.contains(global_addr):
                    return ep_name, ep, ep.addr_range.to_local(global_addr)
        raise RuntimeError(
            f"Global address 0x{global_addr:08x} is not within any slave address "
            f"range on crossbar '{self.name}'"
        )

    def _poll_ep_for_addr(self, global_addr: int) -> tuple[str, MMIFSlave, int]:
        return self._decode_address(global_addr)

    def _dtype(self) -> np.dtype:
        bw = self.bitwidth if self.bitwidth is not None else 32
        return np.dtype(np.uint32) if bw <= 32 else np.dtype(np.uint64)

    def _word_step(self) -> int:
        """Address increment per word (4 for byte-addressed, 1 for word-addressed)."""
        if self.byte_addressable:
            return (self.bitwidth if self.bitwidth is not None else 32) // 8
        return 1

    # ------------------------------------------------------------------
    # Write path
    # ------------------------------------------------------------------

    def write(self, words: Words, global_addr: int, master_port: int,
              tstart: float | None = None) -> ProcessGen[None]:
        ep_name, slave_ep, local_addr = self._decode_address(global_addr)
        protocol = self._slave_protocols.get(ep_name, AXIMMProtocol.FULL)

        if protocol == AXIMMProtocol.FULL:
            # Derate only the per-word (nwords) occupancy term by 1/(1-ov); the
            # fixed init/address latency is untouched (see the model).
            cycles = self.latency_init + words.shape[0] * self._poll_stretch(ep_name)
            dly = cycles / self.clk.freq
            # Early-anchored (pipelined) write: model the burst as having started
            # at `tstart` so it completes at tstart + cycles*period — shorten the
            # wait if the anchor is in the past (mirrors StreamIF.write_pipelined).
            if tstart is not None:
                dly = max(0.0, dly + (tstart - self.env.now))
            if dly > 0:
                yield self.timeout(dly)
            with slave_ep.bus.request() as req:
                yield req
                if slave_ep.rx_write_proc is not None:
                    yield self.env.process(slave_ep.rx_write_proc(words, local_addr))

        else:  # LITE — one word per transaction
            word_step = self._word_step()
            for i in range(words.shape[0]):
                dly = slave_ep.latency_per_word / self.clk.freq
                if tstart is not None and i == 0:
                    dly = max(0.0, dly + (tstart - self.env.now))
                if dly > 0:
                    yield self.timeout(dly)
                with slave_ep.bus.request() as req:
                    yield req
                    if slave_ep.rx_write_proc is not None:
                        yield self.env.process(
                            slave_ep.rx_write_proc(
                                words[i : i + 1],
                                local_addr + i * word_step,
                            )
                        )

        # the watched word may have changed — wake any pollers on this slave
        self._notify_write(ep_name)

    # ------------------------------------------------------------------
    # Read path
    # ------------------------------------------------------------------

    def read(self, nwords: int, global_addr: int, master_port: int) -> ProcessGen[Words]:
        ep_name, slave_ep, local_addr = self._decode_address(global_addr)
        protocol = self._slave_protocols.get(ep_name, AXIMMProtocol.FULL)
        dtype = self._dtype()

        if protocol == AXIMMProtocol.FULL:
            req_dly = self.latency_init / self.clk.freq
            if req_dly > 0:
                yield self.timeout(req_dly)

            with slave_ep.bus.request() as req:
                yield req
                if slave_ep.rx_read_proc is not None:
                    proc = self.env.process(slave_ep.rx_read_proc(nwords, local_addr))
                    yield proc
                    words = proc.value
                else:
                    words = np.zeros(nwords, dtype=dtype)

            # Derate only the per-word (nwords) occupancy term by 1/(1-ov);
            # latency_read_return (the fixed address/return-wire latency) is not.
            ret_dly = (
                self.latency_read_return + nwords * self._poll_stretch(ep_name)
            ) / self.clk.freq
            if ret_dly > 0:
                yield self.timeout(ret_dly)

            return words

        else:  # LITE — one word per transaction
            word_step = self._word_step()
            result = np.zeros(nwords, dtype=dtype)

            for i in range(nwords):
                dly = slave_ep.latency_per_word / self.clk.freq
                if dly > 0:
                    yield self.timeout(dly)
                with slave_ep.bus.request() as req:
                    yield req
                    if slave_ep.rx_read_proc is not None:
                        proc = self.env.process(
                            slave_ep.rx_read_proc(1, local_addr + i * word_step)
                        )
                        yield proc
                        result[i] = proc.value[0]

            return result


# ---------------------------------------------------------------------------
# Direct point-to-point interconnect
# ---------------------------------------------------------------------------

@dataclass
class DirectMMIF(_MMPollSupport, QueuedTransferIF):
    """
    Simple point-to-point MM interconnect: one master, one slave.

    Unlike :class:`AXIMMCrossBarIF`, ``DirectMMIF`` does no address
    translation — the master's address is passed directly to the slave
    callback as ``local_addr``.  This models a component wired directly
    to a BRAM or local register file.

    Endpoint names
    --------------
    ``'master'`` — bind :class:`MMIFMaster`

    ``'slave'`` — bind :class:`MMIFSlave`

    Parameters
    ----------
    latency_write : float
        Cycles of overhead before ``rx_write_proc`` is invoked.
    latency_read : float
        Cycles of overhead on the read request leg.
    latency_read_return : float
        Cycles of overhead on the read return leg (after slave responds).
    byte_addressable : bool
        Addressing convention passed through to the component; has no effect
        on routing within ``DirectMMIF`` itself.
    """

    latency_write: float = 0.
    latency_read: float = 0.
    latency_read_return: float = 0.
    byte_addressable: bool = True

    type_name = 'direct_mmif'

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)

    def __post_init__(self) -> None:
        self.endpoint_names = ('master', 'slave')
        super().__post_init__()
        self._init_poll_support()

    def _poll_ep_for_addr(self, global_addr: int) -> tuple[str, MMIFSlave, int]:
        slave = self.endpoints['slave']
        if slave is None:
            raise RuntimeError("DirectMMIF: slave endpoint is not bound")
        # Point-to-point, no address translation — the master's address is the
        # slave's local address (mirroring read/write below).
        return ('slave', slave, global_addr)

    def bind(self, ep_name: str, endpoint: InterfaceEndpoint) -> None:
        if ep_name == 'master':
            if not isinstance(endpoint, MMIFMaster):
                raise TypeError("master side of DirectMMIF must bind to MMIFMaster")
            self._validate_and_set_bitwidth(endpoint)
            super().bind(ep_name, endpoint)
            endpoint.master_port = 0
        elif ep_name == 'slave':
            if not isinstance(endpoint, MMIFSlave):
                raise TypeError("slave side of DirectMMIF must bind to MMIFSlave")
            self._validate_and_set_bitwidth(endpoint)
            super().bind(ep_name, endpoint)
        else:
            raise KeyError(f"DirectMMIF only has 'master' and 'slave' sides, got '{ep_name}'")

    def _dtype(self) -> np.dtype:
        bw = self.bitwidth if self.bitwidth is not None else 32
        return np.dtype(np.uint32) if bw <= 32 else np.dtype(np.uint64)

    def write(self, words: Words, addr: int, master_port: int,
              tstart: float | None = None) -> ProcessGen[None]:
        slave_ep: MMIFSlave = self.endpoints['slave']
        if slave_ep is None:
            raise RuntimeError("DirectMMIF: slave endpoint is not bound")
        dly = self.latency_write / self.clk.freq
        # Early-anchored (pipelined) write — see AXIMMCrossBarIF.write.
        if tstart is not None:
            dly = max(0.0, dly + (tstart - self.env.now))
        if dly > 0:
            yield self.timeout(dly)
        with slave_ep.bus.request() as req:
            yield req
            if slave_ep.rx_write_proc is not None:
                yield self.env.process(slave_ep.rx_write_proc(words, addr))
        # wake any pollers waiting on this slave (point-to-point: single bus)
        self._notify_write('slave')

    def read(self, nwords: int, addr: int, master_port: int) -> ProcessGen[Words]:
        slave_ep: MMIFSlave = self.endpoints['slave']
        if slave_ep is None:
            raise RuntimeError("DirectMMIF: slave endpoint is not bound")
        dtype = self._dtype()

        req_dly = self.latency_read / self.clk.freq
        if req_dly > 0:
            yield self.timeout(req_dly)

        with slave_ep.bus.request() as req:
            yield req
            if slave_ep.rx_read_proc is not None:
                proc = self.env.process(slave_ep.rx_read_proc(nwords, addr))
                yield proc
                words = proc.value
            else:
                words = np.zeros(nwords, dtype=dtype)

        ret_dly = (self.latency_read_return + nwords) / self.clk.freq
        if ret_dly > 0:
            yield self.timeout(ret_dly)

        return words


# ---------------------------------------------------------------------------
# Address assignment utility
# ---------------------------------------------------------------------------

def assign_address_ranges(
    slaves: list[MMIFSlave],
    ranges: list[tuple[int, int]],
) -> list[AXIMMAddressRange]:
    """
    Assign address ranges to slave endpoints before simulation starts.

    Parameters
    ----------
    slaves : list[MMIFSlave]
        Slave endpoints in port order.
    ranges : list[tuple[int, int]]
        ``(base_addr, size)`` pairs, one per slave.

    Returns
    -------
    list[AXIMMAddressRange]

    Raises
    ------
    ValueError
        If ``len(slaves) != len(ranges)`` or if any two ranges overlap.
    """
    if len(slaves) != len(ranges):
        raise ValueError(
            f"assign_address_ranges: {len(slaves)} slaves but {len(ranges)} ranges"
        )

    addr_ranges = [AXIMMAddressRange(base_addr=b, size=s) for b, s in ranges]

    for i in range(len(addr_ranges)):
        for j in range(i + 1, len(addr_ranges)):
            a, b = addr_ranges[i], addr_ranges[j]
            if a.base_addr < b.base_addr + b.size and b.base_addr < a.base_addr + a.size:
                raise ValueError(
                    f"Address ranges overlap: "
                    f"[0x{a.base_addr:08x}, 0x{a.base_addr + a.size:08x}) and "
                    f"[0x{b.base_addr:08x}, 0x{b.base_addr + b.size:08x})"
                )

    for slave, ar in zip(slaves, addr_ranges):
        slave.addr_range = ar

    return addr_ranges


# ---------------------------------------------------------------------------
# Backward-compat aliases (old names still importable)
# ---------------------------------------------------------------------------

AXIMMCrossBarIFMaster = MMIFMaster
AXIMMCrossBarIFSlave = MMIFSlave
