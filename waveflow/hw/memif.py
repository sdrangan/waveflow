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

from waveflow.hw.interface import (
    InterfaceEndpoint, QueuedTransferIF, TypedCodecMixin, Words, _classify_port_dir,
    port_read, port_write,
)
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
    :class:`~waveflow.hw.memory.MemoryMod` populates it; a slave without one
    cannot be polled."""

    half_duplex: bool = False
    """When ``True`` the read and write channels are the *same* resource, so reads
    and writes to this slave mutually exclude — a declared half-duplex memory port
    (single-port BRAM, or a DDR model that shares R/W bandwidth).  The default
    (``False``, full-duplex) gives independent AR/R and AW/W channels, matching real
    AXI where a read and a write on one bundle never contend."""

    bus_timing: "BusTiming | None" = None
    """Calibrated per-direction bus-occupancy span model for this slave's memory port
    (the contended physical resource).  ``None`` ⇒ the pipelined :class:`Region` slices
    fall back to the plain ``word_bw``-derived span.  Configured per platform at wire-up,
    not poked onto a master by an accelerator."""

    addr_range: AXIMMAddressRange | None = field(init=False)
    read_channel: simpy.Resource = field(init=False)
    write_channel: simpy.Resource = field(init=False)
    write_busy_until: float = field(init=False, default=0.0)

    type_name = 'mmif_slave'

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)

    def __post_init__(self) -> None:
        super().__post_init__()
        self.addr_range = None
        # Per-direction memory-port channels (the contended physical resource): a
        # full-duplex slave has independent AR/R and AW/W channels; a declared
        # half-duplex slave re-couples them onto one shared resource.
        self.read_channel = self.resource(capacity=1)
        self.write_channel = (
            self.read_channel if self.half_duplex else self.resource(capacity=1))
        # Effective time the write channel is busy until — the memory of the last
        # (early-anchored) write's span end.  An early-anchored write measures its span
        # from a *past* anchor (to model within-job read/write overlap), which the SimPy
        # resource alone cannot serialize (it gates the acquire instant, not the span); so
        # ``write_spanned`` clamps each write's anchor to ``max(t_out_start, write_busy_until)``
        # here.  ``0.0`` = free since t=0 (sim time is non-negative, so it never clamps the first).
        self.write_busy_until = 0.0


# ---------------------------------------------------------------------------
# Generic MM master endpoint
# ---------------------------------------------------------------------------

@dataclass
class BusTiming:
    """Per-direction bus-transfer span model owned by an :class:`MMIFSlave` (the
    contended memory port), configured per platform and reached by a :class:`Region`
    slice **through the interconnect**.

    Maps a transfer's **structural facts** to its bus-occupancy span (the time the
    burst holds the channel): ``num_trans`` (the number of bus transactions — e.g.
    one burst per matrix row) and ``nwords`` (the words moved).  The accelerator
    author supplies only those at the slice call (``read_slice_pipelined(...,
    num_trans=n_row)``); the *calibrated* read / write rates live here, on the
    slave — not hand-rolled on the component.

    ``read`` / ``write`` are :class:`~waveflow.calib.CalibModel`-like predictors over
    the feature row ``{"num_trans", "nwords"}`` (anything with
    ``predict(dict) -> cycles``).  ``None`` for a direction means *no model* — the
    slice falls back to the plain word_bw-derived span (the ``num_trans``-less
    behaviour), so an unconfigured master is unaffected.
    """

    read: Any = None
    write: Any = None
    clk_freq: float = 100e6

    def _span_secs(self, model, num_trans, nwords) -> float | None:
        if model is None or num_trans is None:
            return None
        cyc = max(0.0, float(model.predict_feat(
            {"num_trans": float(num_trans), "nwords": float(nwords)})))
        return cyc / float(self.clk_freq)

    def read_span_secs(self, num_trans, nwords) -> float | None:
        """Read-burst span (seconds) for *num_trans* transactions of *nwords* words."""
        return self._span_secs(self.read, num_trans, nwords)

    def write_span_secs(self, num_trans, nwords) -> float | None:
        """Write-burst span (seconds) for *num_trans* transactions of *nwords* words."""
        return self._span_secs(self.write, num_trans, nwords)


@dataclass
class MMIFMaster(TypedCodecMixin, InterfaceEndpoint):
    """
    Master (initiator) endpoint for any MM interconnect.

    Exposes :meth:`write` and :meth:`read` using global addresses, plus
    convenience methods for schema-typed access.

    Bus **contention, occupancy timing, and duplex** are properties of the
    contended memory port, so they live on the :class:`MMIFSlave` (the per-direction
    ``read_channel`` / ``write_channel`` and its :attr:`~MMIFSlave.bus_timing`) and
    are reached **through the interconnect** (which decodes the address to the slave).
    A :class:`Region` slice therefore supplies only the *access pattern* (``num_trans``,
    ``nwords``); it never wires contention by hand, and the master owns no channel or
    span model.
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

    @port_write
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

    @port_read
    def read(self, nwords: int, global_addr: int) -> ProcessGen[Words]:
        """Read *nwords* from *global_addr* and return the word array."""
        self._check_bound()
        proc = self.process(self.interface.read(nwords, global_addr, self.master_port))
        yield proc
        return proc.value

    # ------------------------------------------------------------------
    # Polling (the LT polling-overhead model)
    # ------------------------------------------------------------------

    @port_read
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
        words = yield from self.read(self._typed_nwords(schema_type, word_bw=word_bw), addr)
        return self._unpack(words, schema_type, word_bw=word_bw)

    @port_write
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
        words = self._pack(elements, element_type, count, word_bw=word_bw)
        yield from self.write(words, addr)

    @port_read
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
        words = yield from self.read(
            self._typed_nwords(element_type, count, word_bw=word_bw), addr)
        return self._unpack_elems(words, element_type, count, word_bw=word_bw)

    # ------------------------------------------------------------------
    # Timed (AT) array transfers — sim-only timing capture
    # ------------------------------------------------------------------
    # Same data path as read_array / write_array, but also report the SimPy times that
    # bracket the bus transfer, so a loosely-timed component can record the transaction
    # (or pad to a calibrated schedule) without hand-bracketing env.now.  These are
    # sim-only (no stmt_class) — the timing is an AT-model concern, not synthesizable —
    # mirroring the stream get_pipelined / write_pipelined helpers.
    #
    # **One spelling for a pipelined transfer**, and this is where it was three.  These were
    # `read_array_pipelined` / `write_array_pipelined`; the `_array_` infix carried no
    # information, because every pipelined transfer moves an array — the scalar case has no
    # pipeline to anchor.  So the name is `read_pipelined` / `write_pipelined` here, on
    # `BramIFMaster`, and on `StreamIFMaster`, and the read side of a stream stays
    # `get_pipelined` because a stream read is a destructive dequeue rather than an
    # addressed look.  That last distinction is the one the naming earns; see
    # `plans/interface_docs_and_naming.md` Part 3.

    def read_pipelined(
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
        nwords = self._typed_nwords(element_type, count, word_bw=word_bw)
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
        nwords = self._typed_nwords(element_type, count, word_bw=word_bw)
        data = yield from self.read_array(
            element_type, count, addr, max_count=max_count, word_bw=word_bw)
        period = 1.0 / float(self.interface.clk.freq)
        tstart = self.env.now - max(0, nwords - 1) * period
        return data, tstart

    def write_pipelined(
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
        words = self._pack(elements, element_type, count, word_bw=word_bw)
        nwords = int(np.asarray(words).shape[0])
        tstart = self.env.now
        yield from self.write(words, addr, tstart=t_out_start)
        tend = self.env.now
        return tstart, tend, nwords

    # ------------------------------------------------------------------
    # Spanned (calibrated-occupancy) transfers — delegate the channel + span to
    # the interconnect (which decodes the address to the contended slave port).
    # Sim-only; the pipelined Region slices drive these when the slave declares a
    # bus_timing model.  Same data path as read_array / write_array (pack/unpack).
    # ------------------------------------------------------------------

    def read_spanned(
        self, element_type: type, count: int, addr: int,
        *, t_out_start: float | None, num_trans: int, word_bw: int = 32,
    ) -> ProcessGen[tuple[np.ndarray | list, float, float]]:
        """Read *count* elements through the interconnect's calibrated-occupancy path,
        returning ``(data, t0, t1)`` — the bus-visible span (see
        :meth:`AXIMMCrossBarIF.read_spanned`)."""
        nwords = self._typed_nwords(element_type, count, word_bw=word_bw)
        proc = self.process(self.interface.read_spanned(
            nwords, addr, self.master_port, t_out_start=t_out_start, num_trans=num_trans))
        yield proc
        words, t0, t1 = proc.value
        return self._unpack_elems(words, element_type, count, word_bw=word_bw), t0, t1

    def write_spanned(
        self, elements: Any, element_type: type, addr: int,
        *, t_out_start: float | None, num_trans: int, min_span: float | None,
        count: int | None = None, word_bw: int = 32,
    ) -> ProcessGen[tuple[float, float]]:
        """Write *elements* through the interconnect's calibrated-occupancy path,
        returning the bus-visible span ``(t0, t1)`` (see
        :meth:`AXIMMCrossBarIF.write_spanned`)."""
        words = self._pack(elements, element_type, count, word_bw=word_bw)
        proc = self.process(self.interface.write_spanned(
            words, addr, self.master_port,
            t_out_start=t_out_start, num_trans=num_trans, min_span=min_span))
        yield proc
        return proc.value

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
# ---------------------------------------------------------------------------
# Directional m_axi masters — the direction is the TYPE, not a tag beside it
# ---------------------------------------------------------------------------

class _DirectionalMMIFMaster(MMIFMaster):
    """An :class:`MMIFMaster` that declares which channel direction it uses.

    **Why the type and not a tag.**  Codegen needs to know whether an ``m_axi`` master is read-only
    (``const T*`` + ``#pragma HLS stable``, AR/R channels) or written (``T*``, AW/W/B).  That is not
    visible on a plain :class:`MMIFMaster` — ``MemRStream.m_mem`` and ``MemWStream.m_mem`` are the
    same class — so the fact had to be supplied from the side: a hardcoded table in
    ``mem_stream_gen.top_spec_for``, a ``kind`` string in every composite's ``boundary``, and the
    unused ``as_dir``/``@port_read`` capability view.  Three side-channels for one missing
    declaration.  Declaring it on the type is the same call this codebase already made for execution
    models (``hw_freerun``: *"the execution model is declared by the class ... codegen never has to
    infer"*) and for codegen kinds (*"class states the kind, check() states the fitness"*).  A tag is
    invisible to ``check()``; a type is not.  See ``plans/endpoint_types_not_tags.md``.

    The restriction is **derived, not hand-listed**: the transaction methods are already tagged
    ``@port_read`` / ``@port_write``, so a wrong-direction call is refused by consulting the same
    classifier :class:`CapabilityView` uses.  Adding a method to ``MMIFMaster`` therefore cannot
    leave this class silently permissive.

    Subclasses of :class:`MMIFMaster` on purpose: every ``isinstance(ep, MMIFMaster)`` downstream
    (e.g. ``_discover_mm_masters``) keeps working.  A capability *view* would not — it is a proxy,
    not an ``MMIFMaster``, so the port would vanish from codegen entirely.
    """

    #: ``'R'`` or ``'W'`` — set by the concrete subclass.
    port_dir: ClassVar[str] = 'RW'

    def __getattribute__(self, name: str):
        attr = super().__getattribute__(name)
        if name.startswith('_'):
            return attr
        want = object.__getattribute__(self, '__class__').port_dir
        got = _classify_port_dir(name, attr)
        if got is not None and got != want:
            raise AttributeError(
                f"{type(self).__name__}.{name} is a "
                f"{'read' if got == 'R' else 'write'} operation, but this endpoint declares "
                f"port_dir={want!r}. Use a plain MMIFMaster if the port really is bidirectional — "
                f"the direction is the type, so codegen can lower it without being told separately."
            )
        return attr


class MMIFReadMaster(_DirectionalMMIFMaster):
    """An ``m_axi`` master this component only **reads** through.

    Lowers to ``const T* port`` + ``#pragma HLS stable`` (AR/R channels only).  The ``const`` makes a
    stray write a compile error in the generated C++, and this class makes it an ``AttributeError``
    in the Python model — the same claim, enforced at both levels, declared once.
    """
    port_dir: ClassVar[str] = 'R'


class MMIFWriteMaster(_DirectionalMMIFMaster):
    """An ``m_axi`` master this component only **writes** through.

    Lowers to a plain ``T* port`` (AW/W/B channels).
    """
    port_dir: ClassVar[str] = 'W'


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

    def as_dir(self, direction: str) -> "Region | Any":
        """Return a capability view of this region restricted to *direction*.

        Mirrors :meth:`InterfaceEndpoint.as_dir` for the element-coordinate memory
        view: ``'R'`` exposes ``read_slice`` (blocks ``write_slice``), ``'W'`` the
        inverse, ``'RW'`` the full region.  The interleaver store binds its output
        region as ``'W'`` so a stray read is caught at wire-up."""
        from waveflow.hw.interface import CapabilityView
        if direction not in ('R', 'W', 'RW'):
            raise ValueError(
                f"as_dir: direction must be 'R', 'W', or 'RW', got {direction!r}"
            )
        if direction == 'RW':
            return self
        return CapabilityView(self, direction)

    @property
    def _elem_bytes(self) -> int:
        return (self.element_type.nwords_per_inst(self.word_bw)
                * self.master._word_bytes(self.word_bw))

    def byte_of(self, i: int) -> int:
        """Byte address of element *i* (the element-coordinate → address conversion)."""
        return self.base_addr + int(i) * self._elem_bytes

    def _slave_bus_timing(self, i0: int) -> "BusTiming | None":
        """The :class:`BusTiming` of the slave serving element *i0*, resolved **through the
        interconnect** (which owns the address decode).  ``None`` when the interface is not a
        poll-aware MM interconnect or the slave declares no model — the word_bw fallback."""
        iface = self.master.interface
        resolver = getattr(iface, "slave_endpoint", None)
        if resolver is None:
            return None
        return getattr(resolver(self.byte_of(i0)), "bus_timing", None)

    @port_read
    def read_slice(self, i0: int, i1: int) -> ProcessGen[Any]:
        """Read elements ``[i0, i1)`` (element coordinates) and return the deserialized array —
        the sim twin of ``read_array_slice<W>(mem, i0, i1, x)``.  The interconnect holds the
        slave's read channel for the transfer; fires :attr:`on_transfer`."""
        data, t0, t1, nw = yield from self.master.read_pipelined(
            self.element_type, int(i1) - int(i0), self.byte_of(i0), word_bw=self.word_bw)
        if self.on_transfer is not None:
            self.on_transfer("read", int(i0), nw, t0, t1)
        return data

    @port_write
    def write_slice(
        self, i0: int, elements: Any, element_type: type | None = None,
    ) -> ProcessGen[None]:
        """Write *elements* starting at element index *i0* — the sim twin of
        ``write_array_slice``.  *element_type* defaults to the region's (pass it when the written
        elements differ, e.g. an output format whose addressing stride matches).  The
        interconnect holds the slave's write channel for the transfer; fires :attr:`on_transfer`."""
        t0, t1, nw = yield from self.master.write_pipelined(
            elements, element_type or self.element_type, self.byte_of(i0), word_bw=self.word_bw)
        if self.on_transfer is not None:
            self.on_transfer("write", int(i0), nw, t0, t1)

    @port_read
    def read_slice_pipelined(
        self, i0: int, i1: int, t_out_start: float | None = None, num_trans: int = 1,
    ) -> ProcessGen[tuple[Any, float]]:
        """Read elements ``[i0, i1)`` and return ``(data, tstart)``.

        *num_trans* is the number of bus transactions the read takes (e.g. one burst per
        matrix row).  The **interconnect** turns it into the burst span from the serving
        slave's calibrated :class:`BusTiming` (``span = read model over {num_trans, nwords}``)
        and holds the slave's read channel for **exactly** that span, anchored at *t_out_start*
        (default: now); the functional movement happens in the same call and its own word_bw
        bus time is *subsumed* into the span.  ``tstart`` is the anchor (first-element-available,
        the pipeline-fill time).

        When the serving slave has no :attr:`~MMIFSlave.bus_timing` read model the call falls
        back to the word_bw-derived behaviour: ``tstart`` is back-calculated like
        :meth:`StreamIFSlave.get_pipelined`."""
        n = int(i1) - int(i0)
        nwords = self.element_type.nwords_per_inst(self.word_bw) * n
        bt = self._slave_bus_timing(i0)
        use_span = bt is not None and bt.read is not None and hasattr(
            self.master.interface, "read_spanned")
        if not use_span:
            data, tstart = yield from self.master.read_array_anchored(
                self.element_type, n, self.byte_of(i0), word_bw=self.word_bw)
            t0, t1 = tstart, self.master.env.now
        else:
            data, t0, t1 = yield from self.master.read_spanned(
                self.element_type, n, self.byte_of(i0),
                t_out_start=t_out_start, num_trans=num_trans, word_bw=self.word_bw)
            tstart = t0
        if self.on_transfer is not None:
            self.on_transfer("read", int(i0), nwords, t0, t1)
        return data, tstart

    @port_write
    def write_slice_pipelined(
        self, i0: int, elements: Any, t_out_start: float, num_trans: int = 1,
        element_type: type | None = None, min_span: float | None = None,
    ) -> ProcessGen[tuple[float, float]]:
        """Early-anchored write of *elements* at element *i0*; returns the bus-visible span
        ``(t0, t1)``.

        *num_trans* is the number of bus transactions (e.g. one burst per matrix row).  The
        **interconnect** turns it into the channel-occupancy span from the serving slave's
        calibrated :class:`BusTiming` (``span = write model over {num_trans, nwords}``) and
        holds the slave's write channel for it.

        *min_span* is the **compute-shadow floor** (seconds): in a load-compute-store pipeline
        the store is paced by its producer, so its bus-visible span is ``max(channel_occupancy,
        min_span)`` — when the producer (compute) is the bottleneck the channel hides *under* it
        (``min_span`` > occupancy), and the write completes at ``t_out_start + max(...)``.  The
        component supplies ``min_span`` (the producer pacing); the interconnect owns the channel
        occupancy.  ``None`` means no floor (pure channel occupancy).

        The write completes at ``t_out_start + span`` (clamped to now if the anchor is already
        past), overlapping an earlier read; the functional movement happens in the same call and
        its word_bw bus time is subsumed into the span.  When the serving slave has no
        :attr:`~MMIFSlave.bus_timing` write model *and* no *min_span*, the call falls back to
        the word_bw-derived early-anchored write — the mirror of
        :meth:`StreamIFMaster.write_pipelined`."""
        et = element_type or self.element_type
        nw = et.nwords_per_inst(self.word_bw) * (len(elements) if hasattr(elements, "__len__") else 0)
        bt = self._slave_bus_timing(i0)
        has_model = bt is not None and bt.write is not None
        use_span = (has_model or min_span is not None) and hasattr(
            self.master.interface, "write_spanned")
        if not use_span:
            t0, t1, _ = yield from self.master.write_pipelined(
                elements, et, self.byte_of(i0), word_bw=self.word_bw, t_out_start=t_out_start)
        else:
            t0, t1 = yield from self.master.write_spanned(
                elements, et, self.byte_of(i0), word_bw=self.word_bw,
                t_out_start=t_out_start, num_trans=num_trans, min_span=min_span)
        if self.on_transfer is not None:
            self.on_transfer("write", int(i0), nw, t0, t1)
        return t0, t1


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

    def slave_endpoint(self, global_addr: int) -> MMIFSlave:
        """The :class:`MMIFSlave` serving *global_addr* — the contended memory port
        whose per-direction channels + :attr:`~MMIFSlave.bus_timing` a :class:`Region`
        slice consults through the interconnect (it owns the address decode)."""
        return self._poll_ep_for_addr(global_addr)[1]

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
            with slave_ep.write_channel.request() as req:
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
                with slave_ep.write_channel.request() as req:
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

            with slave_ep.read_channel.request() as req:
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
                with slave_ep.read_channel.request() as req:
                    yield req
                    if slave_ep.rx_read_proc is not None:
                        proc = self.env.process(
                            slave_ep.rx_read_proc(1, local_addr + i * word_step)
                        )
                        yield proc
                        result[i] = proc.value[0]

            return result

    # ------------------------------------------------------------------
    # Spanned (calibrated-occupancy) transfers — the interconnect owns the
    # channel + the per-direction occupancy span (sim-only; the AT model the
    # pipelined Region slices drive).  The functional word-move is *subsumed*
    # into the span; the channel is held for the whole span so successive
    # same-direction transfers and multi-master contention fall out at the
    # contended memory port.
    # ------------------------------------------------------------------

    def read_spanned(
        self, nwords: int, global_addr: int, master_port: int,
        *, t_out_start: float | None, num_trans: int,
    ) -> ProcessGen[tuple[Words, float, float]]:
        """Read *nwords* holding the slave read channel for the calibrated occupancy
        span (``slave.bus_timing`` over ``{num_trans, nwords}``).  Returns
        ``(words, t0, t1)`` — the bus-visible span; ``t0`` is the anchor
        (*t_out_start*, or now) and the read's own word_bw bus time is subsumed."""
        ep_name, slave_ep, local_addr = self._decode_address(global_addr)
        protocol = self._slave_protocols.get(ep_name, AXIMMProtocol.FULL)
        dtype = self._dtype()
        span = slave_ep.bus_timing.read_span_secs(num_trans, nwords)
        with slave_ep.read_channel.request() as req:
            yield req
            anchor = self.env.now if t_out_start is None else float(t_out_start)
            # Functional read (word_bw timing), channel already held — subsumed.
            if protocol == AXIMMProtocol.FULL:
                req_dly = self.latency_init / self.clk.freq
                if req_dly > 0:
                    yield self.timeout(req_dly)
                if slave_ep.rx_read_proc is not None:
                    proc = self.env.process(slave_ep.rx_read_proc(nwords, local_addr))
                    yield proc
                    words = proc.value
                else:
                    words = np.zeros(nwords, dtype=dtype)
                ret_dly = (
                    self.latency_read_return + nwords * self._poll_stretch(ep_name)
                ) / self.clk.freq
                if ret_dly > 0:
                    yield self.timeout(ret_dly)
            else:  # LITE — one word per transaction
                word_step = self._word_step()
                words = np.zeros(nwords, dtype=dtype)
                for i in range(nwords):
                    dly = slave_ep.latency_per_word / self.clk.freq
                    if dly > 0:
                        yield self.timeout(dly)
                    if slave_ep.rx_read_proc is not None:
                        proc = self.env.process(
                            slave_ep.rx_read_proc(1, local_addr + i * word_step))
                        yield proc
                        words[i] = proc.value[0]
            # Occupy the channel out to the full span (anchored).
            t0 = anchor
            t1 = max(anchor + span, self.env.now)
            rem = t1 - self.env.now
            if rem > 0:
                yield self.timeout(rem)
        return words, t0, t1

    def write_spanned(
        self, words: Words, global_addr: int, master_port: int,
        *, t_out_start: float | None, num_trans: int, min_span: float | None,
    ) -> ProcessGen[tuple[float, float]]:
        """Early-anchored write of *words* holding the slave write channel for the
        calibrated occupancy span (``slave.bus_timing`` over ``{num_trans, nwords}``,
        floored by *min_span* — the compute-shadow floor).  Returns the bus-visible
        span ``(t0, t1)``; the write completes at ``anchor + span`` (clamped to now),
        overlapping an earlier read, with its word_bw bus time subsumed.

        *t_out_start* is the caller's *desired* early anchor (deliberately in the past to
        model within-job read/write overlap).  The effective anchor is clamped up to the
        channel's :attr:`~MMIFSlave.write_busy_until` — the span end of the previous write —
        so back-to-back early-anchored writes serialize on the channel instead of
        double-booking it (the SimPy resource gates only the acquire instant, not a
        past-anchored span, so the serialization lives here, not in the caller)."""
        ep_name, slave_ep, local_addr = self._decode_address(global_addr)
        protocol = self._slave_protocols.get(ep_name, AXIMMProtocol.FULL)
        nwords = int(words.shape[0])
        bt = slave_ep.bus_timing
        span = bt.write_span_secs(num_trans, nwords) if bt is not None else None
        if min_span is not None:
            span = max(span if span is not None else 0.0, float(min_span))
        with slave_ep.write_channel.request() as req:
            yield req
            base = self.env.now if t_out_start is None else float(t_out_start)
            anchor = max(base, slave_ep.write_busy_until)   # channel can't restart before last span ended
            # Early-anchored functional write (subsumed), channel already held.
            if protocol == AXIMMProtocol.FULL:
                cycles = self.latency_init + nwords * self._poll_stretch(ep_name)
                dly = max(0.0, cycles / self.clk.freq + (anchor - self.env.now))
                if dly > 0:
                    yield self.timeout(dly)
                if slave_ep.rx_write_proc is not None:
                    yield self.env.process(slave_ep.rx_write_proc(words, local_addr))
            else:  # LITE — one word per transaction
                word_step = self._word_step()
                for i in range(nwords):
                    dly = slave_ep.latency_per_word / self.clk.freq
                    if i == 0:
                        dly = max(0.0, dly + (anchor - self.env.now))
                    if dly > 0:
                        yield self.timeout(dly)
                    if slave_ep.rx_write_proc is not None:
                        yield self.env.process(slave_ep.rx_write_proc(
                            words[i : i + 1], local_addr + i * word_step))
            # the watched word may have changed — wake any pollers on this slave
            self._notify_write(ep_name)
            # Occupy the channel out to the full span (anchored); remember the busy-until so the
            # next early-anchored write on this channel serializes after it.
            t1 = max(anchor + span, self.env.now)
            t0 = t1 - span
            slave_ep.write_busy_until = t1
            rem = t1 - self.env.now
            if rem > 0:
                yield self.timeout(rem)
        return t0, t1


# ---------------------------------------------------------------------------
# Direct point-to-point interconnect
# ---------------------------------------------------------------------------

@dataclass
class DirectMMIF(_MMPollSupport, QueuedTransferIF):
    """
    Simple point-to-point MM interconnect: one master, one slave.

    Unlike :class:`AXIMMCrossBarIF`, ``DirectMMIF`` does no address
    translation — the master's address is passed directly to the slave
    callback as ``local_addr``.  That is the whole delta between the two:
    both are *interconnects*, and this one has a single slave to decode to.

    **Not the way to reach on-chip memory shared between modules.**  A
    ``DirectMMIF`` is a bus link and lowers like one; the accessor stays an
    ``m_axi`` master.  A kernel port that must become a sized ``mode=bram``
    array joined to a memory outside the kernel is
    :class:`~waveflow.hw.bram.BramIF`, which is registered with ``add_rtl_if``
    and keeps its master on the boundary.  What this class actually carries in
    this repo is the AXI4-Lite control link and single-master links to a
    :class:`~waveflow.hw.memory.MemoryMod`.

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
        with slave_ep.write_channel.request() as req:
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

        with slave_ep.read_channel.request() as req:
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
