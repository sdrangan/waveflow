"""rf_sample_if.py — ``RFSampIF``, the RF-domain block-rate sample channel.

**The metronome lives in the edge, not the node** (``plans/adc_model.md``).  ``RFSampIF`` is an
:class:`~waveflow.hw.interface.Interface` — already a :class:`~waveflow.simulation.simobj.SimObj`, so
it already has ``run_proc`` — that owns the sample-rate clock, the block cadence, the producer-side
buffer, and the loss counters.  It is **framework, not example code**: nothing here knows about the
RFDC, or about any converter at all.

**Block-LT, not sample-LT.**  One SimPy event carries one ``(n_ch, blksize)`` block for *every*
channel of a tile.  Splitting per channel would give ``n_ch`` events per block period and work
against the entire reason for block granularity; channel-to-channel skew is instead a **``t0``
vector** on the one interface, which states "these channels share a grid, with these known offsets"
directly rather than as ``n_ch`` declarations that could disagree.

**Unidirectional.**  TX and RX share exactly one quantity — the time origin — and differ in every
other (sample rate, channel count, ``blksize``, buffer, counters, peer).  So there is one interface
per data *direction*: :class:`RFSampIFTx` produces, :class:`RFSampIFRx` consumes.  A genuinely
symmetric case (a TDD antenna port) is a **pair** of interfaces held by one node, which costs
nothing.

**Transport only.**  An edge may own rate, buffering, ordering and loss accounting.  It must **not**
own signal processing: gain, fractional delay and multipath belong in a ``Channel`` block.  Every
behaviour here has to be reproduced by hand in C++ for the XSI realization
(``plans/behavioral_edges.md``) and nothing checks that the two agree, so the bar is "obviously the
same in ten lines".  Zero-fill plus two counters clears it; a filter does not.  Bulk delay is
already :attr:`t0`.

**The counters are the contract, not the zero-fill.**  There is a real asymmetry in the hardware:

- **Buffer full → backpressure on TX.**  Legitimate — the converter has a real input FIFO and it does
  stall the fabric, so :meth:`RFSampIF.put` yields.
- **Buffer empty → underflow.**  There is *no protocol signal for this*.  Nothing in AXI-Stream can
  express "you were late"; the samples simply are not there and the analog output glitches.

Zero-fill is the right filler — deterministic, visible in the RF output, and it does not hide the
error — but the padding is not the contract.  :attr:`~RFSampIF.underrun` and
:attr:`~RFSampIF.overrun` are, and a graph that does not assert them is a design that fails on
hardware and passes in both simulators.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import NamedTuple

import numpy as np
import simpy

from waveflow.hw.clock import Clock
from waveflow.hw.interface import Interface, InterfaceEndpoint, port_read, port_write
from waveflow.simulation.simobj import ProcessGen

#: Default producer-side buffer depth, in **blocks**.  Two is the smallest depth that lets the
#: producer compute block *k+1* while the metronome is draining block *k* — the bounded-lookahead
#: property that replaced the retired master-pull design.
DEFAULT_RF_DEPTH = 2

#: Default receiver-side queue depth, in **blocks** (:attr:`RFSampIFRx.depth`).
DEFAULT_RF_RX_DEPTH = 2


class RfBlock(NamedTuple):
    """One block handed across an :class:`RFSampIF`: its grid index and its samples.

    ``idx`` is the 1-based block number on the interface's absolute grid, so the block covers
    samples ``[(idx-1)*blksize, idx*blksize)`` and completes at ``t_epoch + idx*blk_period``.
    Carrying it beside the data is what lets a receiver place samples on the grid without counting
    arrivals — a dropped block does not shift everything after it.  (Named ``idx`` rather than
    ``index`` so it does not shadow ``tuple.index``.)
    """

    idx: int
    data: np.ndarray


@dataclass
class RFSampIFTx(InterfaceEndpoint):
    """Producer (master) side of an :class:`RFSampIF` — the node that supplies RF blocks.

    Carries **no structural parameters of its own**: ``n_ch``, ``blksize`` and the sample rate live
    on the interface and are read through it.  Two declarations that can disagree is the bug this
    avoids; the same discipline as ``StreamIF.depth``.
    """

    type_name = 'rf_samp_if_tx'

    @port_write
    def put(self, block) -> ProcessGen[None]:
        """Hand one ``(n_ch, blksize)`` block to the interface, **yielding when the buffer is full**.

        Real backpressure: the producer runs at most ``depth`` blocks ahead of the metronome, so an
        RF environment computes with bounded lookahead rather than free-running into memory.
        """
        if self.interface is None:
            raise RuntimeError(
                f"Cannot put: {type(self).__name__} '{self.name}' is not bound to an RFSampIF")
        yield from self.interface.put(block)

    @property
    def n_ch(self) -> int:
        """Channel count — read from the bound interface (never restated here)."""
        return self._iface().n_ch

    @property
    def blksize(self) -> int:
        """Samples per channel per block — read from the bound interface."""
        return self._iface().blksize

    @property
    def samp_rate(self) -> float:
        """Sample rate in Hz — read from the bound interface's clock."""
        return self._iface().samp_rate

    def _iface(self) -> "RFSampIF":
        if self.interface is None:
            raise RuntimeError(
                f"{type(self).__name__} '{self.name}' is not bound to an RFSampIF, so it has no "
                f"n_ch / blksize / samp_rate to read.")
        return self.interface


@dataclass
class RFSampIFRx(InterfaceEndpoint):
    """Consumer (slave) side of an :class:`RFSampIF` — the node that accepts RF blocks.

    **Delivery is non-blocking.**  :meth:`deliver` never yields: the receiver takes the block or the
    block is dropped and counted.  That is the physical truth on the ADC side (the converter presents
    its samples whether or not anyone is ready) and it is also what keeps the metronome honest — a
    blocking receiver would push the grid, and a design that silently slips its sample clock is
    exactly the failure the absolute-grid schedule exists to make impossible.

    :attr:`depth` is this receiver's own input queue, in blocks — a *different* physical buffer from
    the interface's producer-side one, so neither restates the other.
    """

    depth: int = DEFAULT_RF_RX_DEPTH
    type_name = 'rf_samp_if_rx'

    def __post_init__(self) -> None:
        super().__post_init__()
        if int(self.depth) < 1:
            raise ValueError(f"{type(self).__name__}.depth must be >= 1, got {self.depth}")
        self.rx_queue = simpy.Store(self.env, capacity=int(self.depth))

    def deliver(self, blk: RfBlock) -> bool:
        """Offer *blk* to this receiver **without yielding**; ``True`` if it was accepted.

        The default is a bounded queue drained by the owning module's ``run_proc``.  ``False`` means
        the queue was full — the interface counts that as an overrun and drops the block.
        """
        if len(self.rx_queue.items) >= self.rx_queue.capacity:
            return False
        self.rx_queue.put(blk)
        return True

    @port_read
    def get(self) -> ProcessGen[RfBlock]:
        """Pull the next :class:`RfBlock` from this receiver's queue (blocking on the consumer side)."""
        blk = yield self.rx_queue.get()
        return blk

    @property
    def n_ch(self) -> int:
        """Channel count — read from the bound interface (never restated here)."""
        return self._iface().n_ch

    @property
    def blksize(self) -> int:
        """Samples per channel per block — read from the bound interface."""
        return self._iface().blksize

    @property
    def samp_rate(self) -> float:
        """Sample rate in Hz — read from the bound interface's clock."""
        return self._iface().samp_rate

    def _iface(self) -> "RFSampIF":
        if self.interface is None:
            raise RuntimeError(
                f"{type(self).__name__} '{self.name}' is not bound to an RFSampIF, so it has no "
                f"n_ch / blksize / samp_rate to read.")
        return self.interface


@dataclass
class RFSampIF(Interface):
    """A unidirectional, block-rate RF sample channel between one :class:`RFSampIFTx` and one
    :class:`RFSampIFRx`.

    The interface owns the **physical** properties of the channel — the sample clock, the block size,
    the producer-side buffer depth and the time origin — and reads nothing back from its endpoints.
    ``Rfdc`` reads :attr:`samp_rate` from here at bind and pushes :attr:`t0` in the other direction
    (see :meth:`set_t0`); each quantity lives where it physically belongs and is *read*, never
    restated.
    """

    #: The **sample-rate** clock.  This is where the sample rate is declared: a converter reads it at
    #: bind rather than carrying its own copy.
    samp_clk: Clock | None = None
    #: Channels carried per block.  All of a tile's channels ride one interface.
    n_ch: int = 1
    #: Samples per channel per block — the fidelity/speed knob.  Smaller resolves finer timing;
    #: larger runs faster and vectorizes better.
    blksize: int = 1024
    #: Producer-side buffer depth in **blocks**; :meth:`put` yields when it is full.
    depth: int = DEFAULT_RF_DEPTH
    #: Number of block periods the metronome runs, or ``None`` for unbounded (which then needs an
    #: ``env.run(until=...)`` bound).  A testbench sets this so the run terminates on its own.
    n_blk: int | None = None

    type_name = 'rf_samp_if'

    def __post_init__(self) -> None:
        self.endpoint_names = ('tx', 'rx')
        super().__post_init__()
        if self.samp_clk is None:
            raise ValueError(f"samp_clk (the sample-rate Clock) must be provided for "
                             f"{type(self).__name__} '{self.name}'")
        if int(self.n_ch) < 1:
            raise ValueError(f"n_ch must be >= 1, got {self.n_ch}")
        if int(self.blksize) < 1:
            raise ValueError(f"blksize must be >= 1, got {self.blksize}")
        if int(self.depth) < 1:
            raise ValueError(f"depth must be >= 1 block, got {self.depth}")
        self._buf = simpy.Store(self.env, capacity=int(self.depth))
        self._t0: np.ndarray | None = None
        self._t0_owner = None

        # --- the counters: this edge's contract ---------------------------------------------
        #: Blocks the metronome emitted (one per block period it ran).
        self.blocks_sent = 0
        #: Blocks the receiver accepted.
        self.blocks_delivered = 0
        #: Block periods at which the producer had supplied nothing, so a **zero block** went out.
        #: Underflow.  There is no protocol signal for it; this number is the only evidence.
        self.underrun = 0
        #: Blocks the receiver refused (its queue was full) and which were therefore **dropped**.
        self.overrun = 0

    # -- derived rates ---------------------------------------------------------------------------

    @property
    def samp_rate(self) -> float:
        """Sample rate in Hz — ``samp_clk.freq``.  The single declaration; a converter reads it."""
        return float(self.samp_clk.freq)

    @property
    def blk_period(self) -> float:
        """Seconds per block — ``blksize * samp_clk.period``.  (``Clock.period`` is a property.)"""
        return int(self.blksize) * self.samp_clk.period

    # -- t0: the synchronization primitive -------------------------------------------------------

    @property
    def t0(self) -> np.ndarray:
        """Per-channel time origin, shape ``(n_ch,)``: sample *n* of channel *c* occurs at
        ``t0[c] + n / samp_rate``.

        A **vector**, so channel-to-channel skew is stated once on the one interface that carries
        those channels.  Alignment across TX/RX and across antennas is then *derived and assertable*
        (``n_rx / fs_rx == n_tx / fs_tx``) rather than emergent from scheduling coincidence, and it is
        also where MTS lands: a bring-up procedure is not modelable, but its outcome is a measured,
        fixed offset — this one.

        Defaults to all-zeros when nobody set it (a graph with no converter in it).
        """
        if self._t0 is None:
            self._t0 = np.zeros(int(self.n_ch), dtype=float)
        return self._t0

    @property
    def t_epoch(self) -> float:
        """The **block grid anchor**: the earliest channel's origin, ``min(t0)``.

        The transport moves whole ``(n_ch, blksize)`` blocks, so it fires on one grid; the per-channel
        offsets in :attr:`t0` place the *samples* within it.  With an unskewed tile (the common case,
        and what MTS is for) the two coincide.
        """
        return float(np.min(self.t0))

    def set_t0(self, t0, owner=None) -> None:
        """Push the time origin onto this interface — **called by the converter that owns it**.

        ``t0`` is when *the tile's* sample counter starts: a property of the converter, not of a
        wire, so one source sets it for every interface that converter binds.  That is what makes
        TX/RX alignment structural without a bidirectional interface — the two edges share an origin
        because they share the node that assigns it.

        A scalar broadcasts to all channels.  A second, *different* owner setting it is an error, not
        a last-writer-wins: two declarations that can disagree is exactly the bug.
        """
        vec = np.asarray(t0, dtype=float).reshape(-1)
        if vec.size == 1:
            vec = np.full(int(self.n_ch), float(vec[0]))
        if vec.size != int(self.n_ch):
            raise ValueError(
                f"RFSampIF '{self.name}': t0 must be a scalar or a length-{self.n_ch} vector "
                f"(one per channel), got shape {np.shape(t0)}")
        if self._t0 is not None and self._t0_owner is not owner:
            raise ValueError(
                f"RFSampIF '{self.name}': t0 was already set by {self._t0_owner!r} and "
                f"{owner!r} is setting it again. t0 has exactly one owner — the converter whose "
                f"sample counter it describes.")
        self._t0 = vec
        self._t0_owner = owner

    def samp_time(self, ch: int, n: int) -> float:
        """The absolute time of sample *n* on channel *ch*: ``t0[ch] + n / samp_rate``."""
        return float(self.t0[int(ch)]) + int(n) / self.samp_rate

    # -- the producer side -----------------------------------------------------------------------

    def put(self, block) -> ProcessGen[None]:
        """Buffer one ``(n_ch, blksize)`` block, yielding while the buffer is full."""
        arr = np.asarray(block)
        want = (int(self.n_ch), int(self.blksize))
        if arr.shape != want:
            raise ValueError(
                f"RFSampIF '{self.name}': a block must be exactly {want} "
                f"(all channels of the tile, one block period), got {arr.shape}")
        yield self._buf.put(np.array(arr, copy=True))

    @property
    def n_buffered(self) -> int:
        """Blocks currently sitting in the producer-side buffer."""
        return len(self._buf.items)

    # -- the metronome ---------------------------------------------------------------------------

    def run_proc(self) -> ProcessGen[None]:
        """Emit one block every :attr:`blk_period`, **on an absolute grid**.

        Block *k* fires at ``t_epoch + k * blk_period`` — computed from the epoch each time, *not*
        as ``yield timeout(blk_period)`` in a loop.  The difference is not stylistic.  Any yield in
        the body — a blocking push, an edge that charges transfer time, a ``timeout(0)`` in a
        callback — makes the next period start from a later ``env.now`` under the relative form, and
        the grid then slips **cumulatively and silently**.  The non-blocking receiver avoids today's
        obvious case; absolute scheduling makes the property structural rather than one refactor
        away.  ``tests/hw/test_rf_sample_if.py`` demonstrates both halves: the relative loop slipping
        and this one holding, under the same yielding body.
        """
        if self.endpoints['tx'] is None or self.endpoints['rx'] is None:
            raise RuntimeError(
                f"RFSampIF '{self.name}' is not fully bound (tx={self.endpoints['tx']}, "
                f"rx={self.endpoints['rx']}); the metronome has nowhere to draw from or deliver to.")
        t_epoch = self.t_epoch
        period = self.blk_period
        k = 0
        while self.n_blk is None or k < int(self.n_blk):
            k += 1
            dly = t_epoch + k * period - self.env.now
            if dly < 0:
                raise RuntimeError(
                    f"RFSampIF '{self.name}': block {k} was due at {t_epoch + k * period:g}s but "
                    f"the previous block's body did not finish until {self.env.now:g}s — the edge "
                    f"cannot hold its sample grid. Shorten the body or raise blksize.")
            yield self.timeout(dly)
            yield from self._drain_one(k)

    def _drain_one(self, k: int) -> ProcessGen[None]:
        """Emit block *k*: take one block from the buffer (zero-filling on underflow) and offer it to
        the receiver (non-blocking, dropping on overflow).

        Split out from :meth:`run_proc` so the grid discipline and the transfer are separately
        readable — and separately testable: a subclass that makes this body *yield* is how the
        absolute-grid property is demonstrated rather than asserted.
        """
        if self._buf.items:
            block = yield self._buf.get()
        else:
            # Underflow. Zero-fill is deterministic and visible in the RF output; the COUNTER is what
            # anyone is actually allowed to rely on.
            block = np.zeros((int(self.n_ch), int(self.blksize)), dtype=float)
            self.underrun += 1
        self.blocks_sent += 1
        if self.endpoints['rx'].deliver(RfBlock(idx=k, data=block)):
            self.blocks_delivered += 1
        else:
            self.overrun += 1

    # -- reporting -------------------------------------------------------------------------------

    def counters(self) -> dict:
        """The four numbers this edge is accountable for, as a plain dict.

        Shaped for the stage-2 equivalence gate (``plans/behavioral_edges.md`` S4), where the pysim
        and XSI realizations of an edge must produce identical counter tuples for one scenario.
        """
        return {"blocks_sent": self.blocks_sent, "blocks_delivered": self.blocks_delivered,
                "underrun": self.underrun, "overrun": self.overrun}

    def assert_clean(self) -> None:
        """Raise unless ``underrun == 0 and overrun == 0`` — the gate every converter-connected
        example is expected to carry.  Without it a design that fails on hardware passes in both
        simulators."""
        if self.underrun or self.overrun:
            raise AssertionError(
                f"RFSampIF '{self.name}': underrun={self.underrun} overrun={self.overrun} "
                f"(sent={self.blocks_sent} delivered={self.blocks_delivered}). Underrun means the "
                f"producer was late and zero samples went out; overrun means the receiver refused "
                f"blocks and they were dropped.")

    # -- binding ---------------------------------------------------------------------------------

    def bind(self, ep_name: str, endpoint: InterfaceEndpoint) -> None:
        """Bind one side, then give the endpoint's owning module its bind-time hook.

        The hook (``on_rf_bind(iface, ep_name)``, duck-typed on the *module*) is how a converter
        reads :attr:`samp_rate` off this interface and pushes :attr:`t0` back onto it — the two
        opposite-direction reads that keep each quantity single-sourced.  A module that does not
        define it is unaffected.
        """
        if ep_name not in ('tx', 'rx'):
            raise KeyError(
                f"RFSampIF only has 'tx' and 'rx' sides, but got '{ep_name}'")
        if ep_name == 'tx' and not isinstance(endpoint, RFSampIFTx):
            raise TypeError("tx side of RFSampIF must bind to RFSampIFTx")
        if ep_name == 'rx' and not isinstance(endpoint, RFSampIFRx):
            raise TypeError("rx side of RFSampIF must bind to RFSampIFRx")
        super().bind(ep_name, endpoint)
        hook = getattr(getattr(endpoint, 'comp', None), 'on_rf_bind', None)
        if hook is not None:
            hook(self, ep_name)
