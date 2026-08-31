"""reverse_stream.py — the two **reverse-channel** interfaces: credit and ack.

Stage 0 of ``plans/rf_samp_new.md``.  Both are a forward :class:`~waveflow.hw.interface.StreamIF`
plus a *second* stream running the other way, and they are **not two flavours of one mechanism**:

| | :class:`CreditStreamIF` | :class:`AckedStreamIF` |
|---|---|---|
| answers | *"May I send?  Is there room?"* | *"What became of what I sent?"* |
| arrives | **before** the send | **after** the send |
| who could possibly know | the **channel** | only the **consumer** |
| carries | cumulative words consumed | one outcome per **marked** item |

A FIFO already implements credit — ``TREADY`` *is* credit, delivered implicitly one unit at a time
at the moment of use.  An explicit credit channel is nothing but **back-pressure moved earlier and
in bulk**, which is why you only want one when "at the moment of use" is too late: when the producer
commits to a multi-word transaction it cannot abandon partway.  No FIFO can implement an ack,
because what is reported is not a property of the FIFO at all — on the TX side a dropped sample is a
**missed deadline**, delivered perfectly and simply late.

Why they live in one module: the four rules below are genuinely shared, and so is
:func:`udiff`.  Splitting them would duplicate the masking law, which is the one thing in this file
most likely to be got wrong in exactly one of two copies.

Four rules, and each is load-bearing
------------------------------------

**1. Reverse values are cumulative, never incremental.**  A credit value carries *total words
consumed so far*, so a lost one is harmless: the next carries the whole truth.  A lost *increment*
would wedge the producer against a FIFO that looks full and is not.

  Note the honest limit of this rule on the ack side — see :class:`AckedStreamMasterIF`.  A
  ``TxStatus`` *payload* is cumulative, but the **sequence** is not: statuses are one-per-marked-item
  and matched to tokens **positionally**, so losing one mis-pairs every later token permanently.
  That is why :class:`AckedStreamIF` refuses to bind an ack FIFO shallower than ``max_in_flight``
  and why :attr:`AckedStreamMasterIF.n_status_dropped` is expected permanently zero.

**2. Both directions are non-blocking.**  The reverse path uses ``offer``/``get_nb``, never
``write``/``get``.  If it could block it would become a second back-pressure route, which defeats
the entire point.

**3. The reader polls a BOUNDED number, never drain-to-empty.**  :meth:`CreditStreamMasterIF.poll_credit`
and :meth:`AckedStreamMasterIF.harvest` take an ``n`` that is a compile-time constant in the C++
twin, so the poll unrolls into ``n`` ``read_nb`` calls and pipelines.  ``while (got): ...`` is a
data-dependent trip count — the exact construct that costs the current ``RfSampBuf`` design its II
(``HLS 200-878``, ``HLS 200-960``).  **The Python shape is what the C++ twin will be written from**,
so an unbounded loop here would be copied there.

**4. A saturated reverse channel is not stale-but-safe — it is permanently wrong.**  With
``hls::stream`` + ``write_nb`` (pysim: :meth:`~waveflow.hw.interface.StreamIFMaster.offer`) a full
FIFO **drops the newest write while the reader pops the oldest**.  "The newest supersedes" therefore
holds only while the reader outpaces the writer; saturate it and it inverts — the reader receives
ancient values forever and every fresh one is discarded.  That is a correctness property, not a
tuning one, and it is why the ack channel is *solicited* (bounded by construction) rather than
broadcast.

The counters wrap, and that is fine — but only in modular arithmetic
--------------------------------------------------------------------

Every reverse-channel counter is free-running and **will** overflow.  No absolute value is ever
used, only differences, and those are bounded by ``depth``::

    outstanding = (written - acked) & CTR_MASK      # 0 <= outstanding <= depth

A modular subtraction at the counter's width is *exact* whenever the true difference is below
``2**bits``, and ``depth`` is thousands below that at 16 bits.

**The hazard is the Python model, not the hardware.**  ``ap_uint<N>`` wraps by itself; Python ints do
not, so a twin computing ``self.written - self.acked`` on unbounded ints agrees with RTL everywhere
*except* at the boundary.  Every counter difference in this file goes through :func:`udiff`, and
``tests/hw/test_reverse_stream.py`` walks the counters **onto** the wrap rather than near it.

A reverse channel is as wide as what it carries, not as wide as the forward one
--------------------------------------------------------------------------------

Neither reverse channel carries data, so neither is a word wide.  A credit value is a
:data:`CTR_BITS`-wide counter and a status is whatever its :attr:`AckedStreamIF.status_type` says it
is; both used to be built at the *forward* word width, which made the credit channel four times
wider than the number travelling on it and left the ack channel with no declared width at all.

The credit case was not merely wasteful, it was a **disagreement**: the model already computed every
difference at ``ctr_bits`` while the channel was sized at ``bitwidth``, so the two disagreed about
where a value wraps.  One number now settles both — see :attr:`CreditStreamIF.crd_bitwidth`.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, NamedTuple

import numpy as np

from waveflow.hw import arrayutils
from waveflow.hw.dataschema import DataList, DataSchema, IntField
from waveflow.hw.interface import (
    Interface,
    InterfaceEndpoint,
    StreamIF,
    StreamIFMaster,
    StreamIFSlave,
    _block_dtype,
)
from waveflow.hw.synth import sim_only
from waveflow.simulation.simobj import ProcessGen

# ---------------------------------------------------------------------------
# Shared constants and the masking law
# ---------------------------------------------------------------------------

#: Width of a reverse-channel counter — the ``ap_uint<CTR_BITS>`` that travels on the wire.
#: Deliberately *not* the word width: widening the word must not move where a counter wraps, the
#: same separation :data:`waveflow.hw.rf_samp_buf.IDX_BW` makes for the sample index.
CTR_BITS = 16

#: The mask that goes with :data:`CTR_BITS`.  Every difference in this module is taken through it.
CTR_MASK = (1 << CTR_BITS) - 1

#: Words of forward-channel headroom permanently reserved for a **response**.  Data competes for
#: ``depth - RESP_WORDS``; the verdict always fits.  A *dropped* verdict is indistinguishable at the
#: consumer from a hang, which is why it gets reserved room instead of taking its chances.
RESP_WORDS = 1

#: Default number of frames an :class:`AckedStreamMasterIF` may have unresolved at once.  One number
#: governs two things — the ``pending`` FIFO length *and* the minimum ack-channel depth — because a
#: status per accepted frame means the ack FIFO can never need more than this.
MAX_IN_FLIGHT = 4


def udiff(a: int, b: int, bits: int = CTR_BITS) -> int:
    """``a - b`` on a *bits*-wide **unsigned** wrapping counter.

    The pysim twin of the C++ ``(ap_uint<BITS>)(a - b)``.  Exact whenever the true difference is
    below ``2**bits``, which ``depth`` guarantees for every use here.

    Weaker than :func:`waveflow.hw.rf_samp_buf.sdiff` and deliberately so: ``sdiff`` is a *signed*
    three-way compare and needs the two values within ``2**(bits-1)``, because it must decide
    before/at/after.  Here the sign is known — you cannot consume what was not written — so only the
    magnitude is in question and the full range is usable.
    """
    return (int(a) - int(b)) & ((1 << int(bits)) - 1)


def nwords_of(data: Any, word_bw: int) -> int:
    """How many words *data* occupies on a channel of width *word_bw*.

    Accepts the same two forms :meth:`~waveflow.hw.interface.StreamIFMaster.write` does — raw
    ``Words`` or a :class:`~waveflow.hw.dataschema.DataSchema` instance.  The schema path asks the
    generated serializer rather than doing arithmetic on field widths, because the arithmetic is
    right at every width *except* the ones with padding, and nothing would notice.
    """
    if isinstance(data, DataSchema):
        return int(np.asarray(data.serialize(word_bw=int(word_bw))).shape[0])
    return int(np.asarray(data).shape[0])


# ---------------------------------------------------------------------------
# CreditStreamIF — the receiver's channel
# ---------------------------------------------------------------------------


@dataclass
class CreditStreamMasterIF(InterfaceEndpoint):
    """Producer side of a :class:`CreditStreamIF` — the side carrying the never-stall obligation.

    It tracks the room it *knows* it has, so it can offer a write **guaranteed** not to stall.  The
    inversion is the point: the repo's existing progress channels tell the *consumer* where the
    producer is; this tells the *producer* where the consumer is, and the producer is the one that
    may not block.

    **This endpoint is bidirectional by construction** (it writes the forward channel and reads the
    reverse one), so :meth:`~waveflow.hw.interface.InterfaceEndpoint.as_dir` restriction is not
    meaningful for it; bind it ``'RW'``.
    """

    bitwidth: int = 32
    """Width of a forward-channel word."""

    resp_words: int = RESP_WORDS
    """Forward-channel words held back for a response.  See :data:`RESP_WORDS`."""

    ctr_bits: int = CTR_BITS
    """Width of the credit counter on the wire.  Overridable so a test can walk a counter **onto**
    its wrap without simulating 65536 words; the masking law does not depend on the width."""

    fwd_ep: StreamIFMaster = field(init=False)
    """The forward (data) master.  Built here rather than passed in, so the reverse channel cannot
    be wired backwards by a caller — the one mistake that would look like a hang."""

    crd_ep: StreamIFSlave = field(init=False)
    """The reverse (credit) **slave**: the data master is the credit *reader*.

    Built at :attr:`crd_bitwidth`, **not** at :attr:`bitwidth` — see that property."""

    type_name = 'credit_stream_master_if'

    def physical_endpoints(self):
        """Two streams, forward then reverse — there is no credit-stream object in C++."""
        return [self.fwd_ep, self.crd_ep]

    @property
    def crd_bitwidth(self) -> int:
        """Width of a **credit** word — :attr:`ctr_bits`, and derived rather than settable.

        The one number the credit channel is about is the counter, so the channel is exactly as wide
        as the counter.  A second field could disagree with :attr:`ctr_bits`, which is the very
        disagreement this replaces: the accounting masked at 16 bits while the channel was built at
        the forward word width, so widening the *data* silently widened the *counter's* channel
        without moving where the counter wrapped."""
        return int(self.ctr_bits)

    def __post_init__(self) -> None:
        super().__post_init__()
        self.fwd_ep = StreamIFMaster(
            name=f"{self.name}_fwd", sim=self.sim, bitwidth=self.bitwidth, has_tlast=True)
        self.crd_ep = StreamIFSlave(
            name=f"{self.name}_crd", sim=self.sim, bitwidth=self.crd_bitwidth, has_tlast=True)
        #: Cumulative words written to the forward channel, masked at :attr:`ctr_bits`.
        self.written = 0
        #: Cumulative words the consumer has told us it consumed.  A **lower bound** on the truth —
        #: a dropped credit value only makes it staler, never wrong (rule 1).
        self.acked = 0
        #: Writes refused for lack of room.  The counted half of the claim that admission *refuses*
        #: rather than stalls; a run in which it stays zero has not tested that claim.
        self.n_no_room = 0
        #: Responses refused for lack of room.  Expected permanently **zero** — the headroom is
        #: reserved precisely so this cannot happen, so a non-zero value means the reservation was
        #: violated, not that a response was unlucky.
        self.n_resp_no_room = 0

    # -- accounting ---------------------------------------------------------------------------

    @property
    def depth(self) -> int:
        """The forward channel's physical FIFO depth in words.

        Read from the interface, which is the single source for both backends
        (:attr:`waveflow.hw.interface.StreamIF.depth`).  There is no local copy to drift.
        """
        if self.interface is None:
            raise RuntimeError(
                f"{type(self).__name__} '{self.name}' is not bound: there is no channel to have a "
                f"depth, so no credit can be computed")
        return int(self.interface.fwd_if.depth)

    @property
    def outstanding(self) -> int:
        """Words written but not yet known-consumed — ``(written - acked)`` **masked**."""
        return udiff(self.written, self.acked, self.ctr_bits)

    @property
    def avail(self) -> int:
        """Room for **data**, in words.  ``depth - resp_words - outstanding``.

        Conservative in the safe direction: :attr:`acked` lags the truth whenever a credit value is
        in flight or was dropped, so ``avail`` understates the free room and a write it admits can
        never stall.  It is never clamped — a negative value would mean the accounting itself is
        broken, and hiding that behind a ``max(0, ...)`` is how it would survive.
        """
        return self.depth - int(self.resp_words) - self.outstanding

    # -- transactions -------------------------------------------------------------------------

    def poll_credit(self, n: int = 1) -> ProcessGen[int]:
        """Take **up to** *n* credit values; return how many were taken.

        BOUNDED (rule 3): *n* is a compile-time constant in the C++ twin, where this unrolls into
        *n* ``read_nb`` calls and pipelines at II=1.  **Never** ``while (got): ...``.

        Cumulative, so the newest value simply overwrites — no accumulation, no ordering logic, and
        a value that never arrives costs nothing but staleness (rule 1).
        """
        ntaken = 0
        for _ in range(int(n)):
            got = yield from self.crd_ep.get_nb()
            if got is None:
                break
            self.acked = int(np.asarray(got).reshape(-1)[-1]) & ((1 << int(self.ctr_bits)) - 1)
            ntaken += 1
        return ntaken

    def write_nb(self, words: Any) -> ProcessGen[bool]:
        """Write *words* if the credit accounting says they fit; otherwise refuse.  **Never blocks.**

        Returns ``True`` when the write happened.  ``False`` costs the caller the transaction and
        nothing else — which is the whole reason a producer that cannot be stalled can use this.

        The write itself is the **blocking** :meth:`~waveflow.hw.interface.StreamIFMaster.write`,
        and that is deliberate: the credit reservation already proved the room, so a stall here is
        impossible unless the accounting is wrong — in which case the run deadlocks loudly instead of
        silently dropping a burst the caller believed had been admitted.
        """
        n = nwords_of(words, self.bitwidth)
        if n > self.avail:
            self.n_no_room += 1
            return False
        yield from self.fwd_ep.write(words)
        self.written = (self.written + n) & ((1 << int(self.ctr_bits)) - 1)
        return True

    def write_resp_nb(self, resp: Any) -> ProcessGen[bool]:
        """Write a **response**, drawing on the reserved headroom so it cannot be refused for room.

        Returns ``False`` only if the reservation was somehow violated, which is unreachable by
        construction — hence :attr:`n_resp_no_room`, which exists to be asserted zero rather than
        inspected.
        """
        n = nwords_of(resp, self.bitwidth)
        if n > int(self.resp_words):
            raise ValueError(
                f"response of {n} words exceeds the reserved headroom of {self.resp_words}: raise "
                f"resp_words rather than letting a verdict compete with data for room")
        if self.depth - self.outstanding < n:
            self.n_resp_no_room += 1
            return False
        yield from self.fwd_ep.write(resp)
        self.written = (self.written + n) & ((1 << int(self.ctr_bits)) - 1)
        return True


@dataclass
class CreditStreamSlaveIF(InterfaceEndpoint):
    """Consumer side of a :class:`CreditStreamIF`: consume, then say how much in total.

    The offer is non-blocking (rule 2) and the value is cumulative (rule 1), so this side can never
    stall the producer and never has to care whether a particular value arrived.
    """

    bitwidth: int = 32
    """Width of a forward-channel word."""

    ctr_bits: int = CTR_BITS
    """Width of the credit counter on the wire.  Must match the master's."""

    queue_size: int | None = None
    """Forward RX queue depth in words.  ``None`` — the normal case — lets
    :meth:`waveflow.hw.interface.StreamIF.bind` apply the channel's own ``depth``, which is what
    makes :attr:`CreditStreamMasterIF.depth` and the real queue the *same* number."""

    fwd_ep: StreamIFSlave = field(init=False)
    crd_ep: StreamIFMaster = field(init=False)

    type_name = 'credit_stream_slave_if'

    def physical_endpoints(self):
        return [self.fwd_ep, self.crd_ep]

    @property
    def crd_bitwidth(self) -> int:
        """Width of a credit word — :attr:`ctr_bits`.  See
        :attr:`CreditStreamMasterIF.crd_bitwidth`; the two sides read the same number off the same
        field, which is why they cannot be built at different widths."""
        return int(self.ctr_bits)

    def __post_init__(self) -> None:
        super().__post_init__()
        self.fwd_ep = StreamIFSlave(
            name=f"{self.name}_fwd", sim=self.sim, bitwidth=self.bitwidth,
            has_tlast=True, queue_size=self.queue_size)
        self.crd_ep = StreamIFMaster(
            name=f"{self.name}_crd", sim=self.sim, bitwidth=self.crd_bitwidth, has_tlast=True)
        #: Cumulative words consumed, masked at :attr:`ctr_bits` — exactly what goes on the wire.
        self.consumed = 0

    # Three readers, mirroring `StreamIFSlave`'s three, so this stays a drop-in for a plain stream
    # slave — the credit offer is the only addition.  They split for the same reason the stream's
    # did: one method returning `Words`, an instance, or a `DataArray` depending on its arguments.
    # The credit accounting is identical in all three and lives in `_consume`, because what is
    # credited is WORDS — `nwords_of` asks the serializer rather than counting instances, which is
    # the difference that is invisible at one word per instance and wrong everywhere else.

    def get(self, *, nwords_max: int | None = None):
        """Consume a raw burst, then offer the **new cumulative total** back."""
        data = yield from self.fwd_ep.get(nwords_max=nwords_max)
        yield from self._consume(data)
        return data

    def get_schema(self, schema_type):
        """Consume one instance of *schema_type*, then offer the new cumulative total back."""
        data = yield from self.fwd_ep.get_schema(schema_type)
        yield from self._consume(data)
        return data

    def get_array(self, element_type, count):
        """Consume *count* elements of *element_type*, then offer the new cumulative total back."""
        data = yield from self.fwd_ep.get_array(element_type, count)
        yield from self._consume(data)
        return data

    def _consume(self, data) -> ProcessGen[None]:
        """Charge *data* to the cumulative counter and offer it.

        The offer uses :meth:`~waveflow.hw.interface.StreamIFMaster.offer`, so a full reverse FIFO
        **discards** the value and counts it on the reverse interface.  That is rule 4 in the flesh:
        harmless while the reader outpaces the writer, and inverted the moment it does not.
        """
        n = nwords_of(data, self.bitwidth)
        self.consumed = (self.consumed + n) & ((1 << int(self.ctr_bits)) - 1)
        yield from self.offer_credit()

    def offer_credit(self) -> ProcessGen[int]:
        """Offer the current cumulative total on the reverse channel.  Non-blocking; may be dropped.

        Separate from :meth:`get` so a consumer that reads the forward channel some other way (a
        typed read, a drain) can still keep the credit channel honest without re-implementing it.
        """
        word = np.array([self.consumed], dtype=_block_dtype(self.crd_bitwidth))
        return (yield from self.crd_ep.offer(word))


@dataclass
class CreditStreamIF(Interface):
    """A forward stream plus a reverse stream carrying **cumulative words consumed**.

    Two :class:`~waveflow.hw.interface.StreamIF` channels held by one interface rather than a new
    protocol: in C++ this is *literally* a pair of ``hls::stream`` plus two registers in the
    producer, so nothing new has to be shown to work — which is the main practical argument for this
    over ``stream_of_blocks``.

    Both sub-channels are built here.  The reverse channel's master is the **data slave**, and
    getting that backwards is the one wiring mistake that would present as a hang rather than an
    error, so :meth:`bind` does it and callers do not.
    """

    bitwidth: int = 32
    clk: Any = None
    """Clock for both sub-channels.  Required (a ``StreamIF`` cannot be built without one)."""

    depth: int = 8
    """Forward-channel FIFO depth in words.  Unlike a plain ``StreamIF`` this may **not** be
    ``None``: ``avail`` is ``depth - resp_words - outstanding``, and an unbounded queue has no
    ``depth`` to subtract from.  A credit channel without a depth is a credit channel without
    credit."""

    credit_depth: int = 4
    """Reverse-channel FIFO depth in words.  Rule 4 lives here: sized so the *reader* (the producer's
    :meth:`~CreditStreamMasterIF.poll_credit`) outpaces the writer.  There is no structural guarantee
    on this side — only a rate argument — so a consumer that ever acks per word needs the solicited
    treatment :class:`AckedStreamIF` has."""

    ctr_bits: int = CTR_BITS
    """Width of the credit counter, which is also the width of the credit **channel**.

    Here as well as on the endpoints because :attr:`crd_if` is built in ``__post_init__``, before any
    endpoint exists to read it from — the same reason :attr:`bitwidth` is stated three times.  All
    three must agree, and :meth:`bind` refuses them when they do not."""

    fwd_if: StreamIF = field(init=False)
    crd_if: StreamIF = field(init=False)

    type_name = 'credit_stream_if'

    def physical_interfaces(self):
        """Two ordinary streams.  Nothing here lowers to a new kind of edge."""
        return [self.fwd_if, self.crd_if]

    @property
    def crd_bitwidth(self) -> int:
        """Width of the reverse channel — :attr:`ctr_bits`, **not** :attr:`bitwidth`.

        Derived, so the channel and the arithmetic cannot disagree.  It is also what the generated
        FIFO is built at: :meth:`physical_interfaces` hands :attr:`crd_if` to
        :func:`~waveflow.build.composite_gen.derive_internal_edges`, which reads the width off the
        ``StreamIF`` — so a credit channel that was 64 bits in pysim was 64 bits in RTL too, for a
        16-bit number."""
        return int(self.ctr_bits)

    def __post_init__(self) -> None:
        self.endpoint_names = ('master', 'slave')
        if self.clk is None:
            raise ValueError(f"clock must be provided for {type(self).__name__}")
        if self.depth is None:
            raise ValueError(
                "CreditStreamIF.depth may not be None: credit is 'depth - outstanding', and an "
                "unbounded queue has no depth to compute it from")
        super().__post_init__()
        self.fwd_if = StreamIF(name=f"{self.name}_fwd", sim=self.sim, clk=self.clk,
                               bitwidth=self.bitwidth, depth=int(self.depth))
        self.crd_if = StreamIF(name=f"{self.name}_crd", sim=self.sim, clk=self.clk,
                               bitwidth=self.crd_bitwidth, depth=int(self.credit_depth))

    @property
    def n_credit_dropped(self) -> int:
        """Credit values discarded by a saturated reverse FIFO.

        Non-zero is *not* automatically a fault here — a dropped cumulative value self-heals — but
        it is the measurement behind the rate argument, so a design that relies on the argument
        should watch it rather than assume it.
        """
        return int(self.crd_if.dropped)

    def bind(self, ep_name: str, endpoint: InterfaceEndpoint) -> None:
        if ep_name not in ('master', 'slave'):
            raise KeyError(
                f"CreditStreamIF has only 'master' and 'slave' sides, but got '{ep_name}'")
        want = CreditStreamMasterIF if ep_name == 'master' else CreditStreamSlaveIF
        if not isinstance(endpoint, want):
            raise TypeError(f"'{ep_name}' side of CreditStreamIF must bind to {want.__name__}")
        # BEFORE the sub-interface binds, because `StreamIF.bind` would otherwise reject the same
        # mismatch as a bare width disagreement -- true, but naming the symptom rather than the
        # cause.  The counter width and the channel width are one number now, so a wrong `ctr_bits`
        # presents as a wrong channel width, and this is the message that says which it is.
        if int(endpoint.ctr_bits) != int(self.ctr_bits):
            raise ValueError(
                f"counter widths differ across '{self.name}': the channel is built at "
                f"{self.ctr_bits} bits and the '{ep_name}' endpoint masks at {endpoint.ctr_bits}.  "
                f"The two must mask identically or they agree everywhere except at the wrap")
        if ep_name == 'master':
            self.fwd_if.bind('master', endpoint.fwd_ep)
            self.crd_if.bind('slave', endpoint.crd_ep)
        else:
            self.fwd_if.bind('slave', endpoint.fwd_ep)
            self.crd_if.bind('master', endpoint.crd_ep)
        super().bind(ep_name, endpoint)
        self._check_bound()

    def _check_bound(self) -> None:
        """Validate what only becomes checkable once both sides are present.

        The counter width is **not** checked here: it is checked in :meth:`bind` against the
        interface's own :attr:`ctr_bits`, which both endpoints are compared to, and which has to
        happen before the sub-interface bind rather than after it.  Two endpoints that each match the
        channel match each other.
        """
        m, s = self.endpoints['master'], self.endpoints['slave']
        if m is None or s is None:
            return
        if int(self.depth) <= int(m.resp_words):
            raise ValueError(
                f"CreditStreamIF '{self.name}': depth={self.depth} leaves no room for data once "
                f"resp_words={m.resp_words} is reserved")


# ---------------------------------------------------------------------------
# AckedStreamIF — the transmitter's channel
# ---------------------------------------------------------------------------


class MarkedRead(NamedTuple):
    """One item off an :class:`AckedStreamIF` forward channel, with its mark bit."""

    item: int
    mark: int


@lru_cache(maxsize=None)
def marked_word_type(bitwidth: int) -> type[DataList]:
    """The one-word beat an :class:`AckedStreamIF` forward channel carries: payload + mark bit.

    The mark travels **in band**, which is the whole point — a side channel carrying "which item was
    marked" would be a second stream to keep in step, and keeping two streams in step is the defect
    the tag-with-the-sample decision deletes (``plans/rf_samp_new.md``, *Tagged samples*).

    Built through :class:`~waveflow.hw.dataschema.DataList` so packing and unpacking go through the
    generated serializers rather than hand-rolled shifts; a hand-rolled pack is right at every width
    until it is not, and nothing notices.  The payload takes ``bitwidth - 1`` bits so a beat is
    exactly one word — the same shape ``TaggedSamp`` has in the C++ twin, where ``request_status``
    is one bit of the struct.
    """
    bw = int(bitwidth)
    if bw < 2:
        raise ValueError(f"bitwidth={bw} leaves no room for a payload beside the mark bit")
    cls = type(
        f"MarkedWord{bw}",
        (DataList,),
        {
            "__doc__": f"A {bw}-bit beat: {bw - 1} bits of payload plus a 1-bit mark.",
            "elements": {
                "data": {"schema": IntField.specialize(bitwidth=bw - 1, signed=False),
                         "description": "payload"},
                "mark": {"schema": IntField.specialize(bitwidth=1, signed=False),
                         "description": "request a status when this item resolves"},
            },
        },
    )
    nw = cls.nwords_per_inst(bw)
    if nw != 1:
        raise ValueError(f"marked word packs to {nw} words at bitwidth={bw}, expected 1")
    return cls


@dataclass
class AckedStreamMasterIF(InterfaceEndpoint):
    """Producer side of an :class:`AckedStreamIF`: frames in, resolved tokens out.

    **The pending FIFO lives here, not in the app.**  Its ordering guarantee — one status per marked
    item, in the order the marks were sent — is what makes token recovery correct with no id on the
    wire.  Every user would otherwise hand-roll the same queue, and the failure when they get it
    wrong is silent: a token paired with the wrong frame's verdict looks exactly like a verdict.

    **Where rule 1 stops.**  A ``TxStatus`` payload is cumulative, so its *contents* self-heal.  The
    *sequence* does not: statuses are matched to tokens positionally, so a single dropped status
    mis-pairs every later one, permanently and silently.  This is why the ack channel is solicited
    (one status per accepted frame, so the FIFO can be **sized** rather than hoped at) and why
    :attr:`n_status_dropped` is expected permanently zero.  :class:`AckedStreamIF` refuses at bind
    time to build a channel where it could be non-zero.
    """

    bitwidth: int = 32
    """Width of a forward-channel word (payload + mark share one beat)."""

    max_in_flight: int = MAX_IN_FLIGHT
    """Frames that may be unresolved at once — the ``pending`` FIFO length.  The same number bounds
    the ack channel's depth, because a solicited status arrives at most one per accepted frame."""

    fwd_ep: StreamIFMaster = field(init=False)
    ack_ep: StreamIFSlave = field(init=False)

    type_name = 'acked_stream_master_if'

    def physical_endpoints(self):
        """Two streams, forward then ack.  **This order is the C++ argument order** — a task body
        taking this endpoint takes ``(fwd, ack)`` adjacent, in that sequence."""
        return [self.fwd_ep, self.ack_ep]

    def __post_init__(self) -> None:
        super().__post_init__()
        self.fwd_ep = StreamIFMaster(
            name=f"{self.name}_fwd", sim=self.sim, bitwidth=self.bitwidth, has_tlast=True)
        self.ack_ep = StreamIFSlave(
            name=f"{self.name}_ack", sim=self.sim, bitwidth=self.bitwidth, has_tlast=True)
        self._word_type = marked_word_type(self.bitwidth)
        #: Tokens of frames written and not yet resolved, oldest first.  **The** ordering guarantee.
        self._pending: deque = deque()
        #: Statuses that arrived with no pending frame to pair them with.  Unreachable while the
        #: contract holds; counted rather than raised so a diagnosis sees the count *and* the run.
        self.n_orphan_status = 0
        #: Frames written.  Used by nothing here — it is the denominator a caller needs to say
        #: "every frame resolved".
        self.n_frames = 0

    # -- the admission condition ---------------------------------------------------------------

    def can_write_frame(self) -> bool:
        """Is a pending slot free?  **An admission condition, not a convenience.**

        Without it the app either blocks on a full pending FIFO — coupling the producer to the
        consumer's progress and deadlocking if the consumer never resolves — or accepts a frame it
        cannot remember, which breaks the token correspondence silently.  The contract is
        **check, then write**, and :meth:`write_frame` asserts it rather than trusting it.

        **No ``_nb`` suffix, and that is the convention rather than an exception to it.**  ``_nb``
        marks a *transfer* that returns "nothing available" or "no room" instead of blocking —
        :meth:`AckedStreamSlaveIF.read_nb`, :meth:`read_frame_nb`,
        :meth:`~waveflow.hw.interface.StreamIFSlave.get_nb`.  This moves nothing and answers a
        question; a predicate never blocks, so the suffix would carry no information here.  What it
        gates is :meth:`write_frame`, which *does* block, and which is therefore not ``_nb`` either.
        """
        return len(self._pending) < int(self.max_in_flight)

    @property
    def n_pending(self) -> int:
        """Frames written and not yet resolved."""
        return len(self._pending)

    @property
    def n_status_dropped(self) -> int:
        """Statuses discarded by a saturated ack FIFO.  **Expected permanently zero.**

        Non-zero does not mean a verdict was merely lost — it means every later token is paired with
        the wrong frame's verdict.  It is a sizing violation, which is worth hearing loudly rather
        than debugging as a lost status.
        """
        if self.interface is None:
            return 0
        return int(self.interface.ack_if.dropped)

    # -- transactions --------------------------------------------------------------------------

    def write_frame(self, words: Any, token: Any) -> ProcessGen[None]:
        """Write *words* as one frame, **marking the last item**, and remember *token*.

        One frame is one burst (one TLAST packet), which is what gives the consumer a boundary and
        :meth:`AckedStreamSlaveIF.read_frame_nb` something to read.

        Raises if :meth:`can_write_frame` is false.  That is the point: accepting a frame it cannot
        remember is the silent-correspondence bug, so the twin refuses rather than trusting.

        Raises on an **empty** frame too.  A zero-length frame has no last item, so no mark is sent,
        so no status returns and the pending slot never pops — a few of those and the producer
        refuses everything for reasons that look nothing like the cause
        (``plans/rf_samp_new.md``, *Stage 1*, and the ``nsamp == 0`` open question).  Refusing here
        is one of the two answers that plan demands; it is not left undefined.
        """
        if not self.can_write_frame():
            raise RuntimeError(
                f"{type(self).__name__} '{self.name}': write_frame() with no free pending slot "
                f"({len(self._pending)}/{self.max_in_flight} in flight).  The contract is "
                f"'check, then write' — call can_write_frame() and treat False as an admission "
                f"refusal.  Accepting this frame would break the token/status correspondence "
                f"silently, which is worse than refusing it loudly.")
        payload = list(np.asarray(words).reshape(-1))
        if not payload:
            raise ValueError(
                f"{type(self).__name__} '{self.name}': an empty frame has no last item to mark, so "
                f"no status ever returns and the pending slot leaks.  Refuse nsamp == 0 upstream.")
        raw = self._pack(payload, mark_last=True)
        yield from self.fwd_ep.write(raw)
        self._pending.append(token)
        self.n_frames += 1

    def harvest(self, n: int = MAX_IN_FLIGHT) -> ProcessGen[list[tuple[Any, int]]]:
        """Take **up to** *n* statuses; return ``[(token, status), ...]`` oldest first.

        BOUNDED (rule 3) — *n* is a compile-time constant in the C++ twin and unrolls into *n*
        ``read_nb`` calls.  **Never** ``while (got): ...``.

        Returns a list rather than yielding items one at a time: this is a SimPy process, so
        ``yield`` is already spoken for by the event loop, and the C++ twin fills a fixed-size array
        for the same reason it cannot have an unbounded loop.  (The plan writes ``harvest`` as
        "yields (token, status)"; a generator-of-results is not expressible here, and a list of at
        most *n* is the shape the hardware has anyway.)

        Pairing is **positional** — the oldest pending token takes the oldest status.  Nothing is
        matched by id because nothing carries one; that is what the ordering guarantee buys, and
        what :attr:`n_status_dropped` protects.
        """
        out: list[tuple[Any, int]] = []
        for _ in range(int(n)):
            got = yield from self.ack_ep.get_nb()
            if got is None:
                break
            status = int(np.asarray(got).reshape(-1)[-1])
            if not self._pending:
                self.n_orphan_status += 1
                continue
            out.append((self._pending.popleft(), status))
        return out

    def assert_clean(self) -> None:
        """Raise unless the two counters that must be zero are zero.

        The gate a caller should run at the end of a sim: ``n_status_dropped == 0`` is a *sizing*
        claim and ``n_orphan_status == 0`` a *correspondence* one, and neither is visible in the
        data.
        """
        if self.n_status_dropped:
            raise AssertionError(
                f"{self.name}: {self.n_status_dropped} status word(s) dropped by a saturated ack "
                f"FIFO.  Every token resolved after the first drop is paired with the wrong "
                f"frame's verdict.  Size the ack channel >= max_in_flight.")
        if self.n_orphan_status:
            raise AssertionError(
                f"{self.name}: {self.n_orphan_status} status word(s) arrived with no pending frame")

    # -- packing -------------------------------------------------------------------------------

    def _pack(self, payload: list[int], mark_last: bool) -> np.ndarray:
        """Pack *payload* into one word per item, marking the last if asked."""
        wt = self._word_type
        items = []
        for i, v in enumerate(payload):
            it = wt()
            it.data = int(v)
            it.mark = int(bool(mark_last and i == len(payload) - 1))
            items.append(it)
        return arrayutils.write_array(arrayutils.array(wt, items), word_bw=self.bitwidth)


@dataclass
class AckedStreamSlaveIF(InterfaceEndpoint):
    """Consumer side of an :class:`AckedStreamIF` — **two readers, only one of them synthesizable.**

    :meth:`read_nb` is per-item because the hardware consumer is metronome-paced: it takes one
    sample per slot and decides on each, so it can never consume a frame in one go.
    :meth:`read_frame_nb` is the **LT approximation** — one SimPy event per frame instead of one per
    sample, which is what makes a millisecond of signal simulable — and it is ``@sim_only``.

    The approximation is smaller than it looks: the status is emitted only for the *marked* item, so
    the RTL verdict already answers "did the last sample make it?", not "did the whole frame?".  What
    diverges is a per-slot count like ``n_underrun``, which is the already-declared block-granularity
    limit, inherited rather than introduced.
    """

    bitwidth: int = 32
    slot_period: float | None = None
    """Seconds one item occupies at the consumer's metronome.  Required by :meth:`read_frame_nb` and
    unused by :meth:`read_nb` (whose caller does its own pacing)."""

    queue_size: int | None = None

    fwd_ep: StreamIFSlave = field(init=False)
    ack_ep: StreamIFMaster = field(init=False)

    type_name = 'acked_stream_slave_if'

    def physical_endpoints(self):
        return [self.fwd_ep, self.ack_ep]

    def __post_init__(self) -> None:
        super().__post_init__()
        self.fwd_ep = StreamIFSlave(
            name=f"{self.name}_fwd", sim=self.sim, bitwidth=self.bitwidth,
            has_tlast=True, queue_size=self.queue_size)
        self.ack_ep = StreamIFMaster(
            name=f"{self.name}_ack", sim=self.sim, bitwidth=self.bitwidth, has_tlast=True)
        self._word_type = marked_word_type(self.bitwidth)
        #: Items unpacked from the burst currently being read one at a time by :meth:`read_nb`.
        self._items: deque[MarkedRead] = deque()
        #: Statuses sent.  The denominator for "one per marked item".
        self.n_status = 0

    def read_nb(self) -> ProcessGen[MarkedRead | None]:
        """Take **one** item, or ``None`` if none is available.  Never blocks.  **The HLS twin.**

        Per-item by design: this is the shape the C++ body has, where the beat is one
        ``TaggedSamp`` and the decision is taken on it alone.  A burst arrives as one SimPy event
        (that is the pysim transport unit — a frame is a TLAST packet), and this hands it out one
        item at a time; the caller supplies its own inter-item pacing, which is the metronome.
        """
        if not self._items:
            burst = yield from self.fwd_ep.get_nb()
            if burst is None:
                return None
            self._items.extend(self._unpack(burst))
            if not self._items:
                return None
        return self._items.popleft()

    def send_status(self, payload: int) -> ProcessGen[int]:
        """Emit one status.  Non-blocking (rule 2); **one per marked item**, never unsolicited.

        Uses :meth:`~waveflow.hw.interface.StreamIFMaster.offer`, so a full FIFO discards it and the
        interface counts it — see :attr:`AckedStreamMasterIF.n_status_dropped` for why that count is
        a sizing violation rather than a lost verdict.
        """
        self.n_status += 1
        word = np.array([int(payload)], dtype=_block_dtype(self.bitwidth))
        return (yield from self.ack_ep.offer(word))

    @sim_only
    def read_frame_nb(self) -> ProcessGen[list[MarkedRead] | None]:
        """Take a **whole frame**, charging its playout before returning.  PYSIM ONLY.

        **The charge is the whole point.**  A frame read that reports immediately hands the producer
        a verdict *before those items would have played*, so the producer runs ahead of what the
        hardware allows and every rate conclusion drawn from the model is optimistic.  That is the
        same defect that made the RX ingress twin report 0 dropped against the hardware's 1695
        (PR #160): a twin that consumes a burst per firing and charges nothing is rate-blind, and
        rate-blind twins report zero loss where the hardware loses samples.

        Take first (non-blocking), **then** charge, then report.
        """
        if self._items:
            raise RuntimeError(
                f"{type(self).__name__} '{self.name}': read_frame_nb() with {len(self._items)} "
                f"item(s) left over from read_nb().  The two readers are alternatives — one is the "
                f"HLS twin, the other its LT approximation — and mixing them would split one frame "
                f"across two granularities with two different notions of when it played.")
        if self.slot_period is None:
            raise RuntimeError(
                f"{type(self).__name__} '{self.name}': read_frame_nb() needs slot_period to charge "
                f"the playout.  Reporting a frame for free is exactly the rate-blindness this "
                f"reader exists to avoid, so there is no default.")
        burst = yield from self.fwd_ep.get_nb()
        if burst is None:
            return None
        items = self._unpack(burst)
        yield self.timeout(len(items) * float(self.slot_period))
        return items

    def _unpack(self, burst: np.ndarray) -> list[MarkedRead]:
        """Unpack a burst of one-word beats into ``(item, mark)`` pairs."""
        raw = np.asarray(burst).reshape(-1)
        n = int(raw.shape[0])
        if n == 0:
            return []
        arr = arrayutils.read_array(raw, elem_type=self._word_type,
                                    word_bw=self.bitwidth, shape=n)
        out = []
        for i in range(n):
            elem = arr[i]
            out.append(MarkedRead(int(elem["data"]), int(elem["mark"])))
        return out


@dataclass
class AckedStreamIF(Interface):
    """A forward stream plus a reverse stream carrying **one outcome per marked item**.

    The producer needs no credit here because **it is allowed to block**: back-pressure costs it
    time and nothing else.  That asymmetry — the RX producer cannot be stalled (physics), the TX
    producer can (it is only logic) — is what selects a different reverse channel on each side.
    """

    bitwidth: int = 32
    clk: Any = None
    depth: int = 16
    """Forward-channel FIFO depth in words."""

    ack_depth: int | None = None
    """Reverse-channel FIFO depth in words.  ``None`` takes the master's ``max_in_flight``, which is
    the sizing rule: exactly one status per accepted frame means the ack FIFO can never need more.
    **One number governs both** the pending FIFO and this depth, and :meth:`bind` refuses a channel
    where they disagree in the unsafe direction."""

    fwd_if: StreamIF = field(init=False)
    ack_if: StreamIF = field(init=False)

    type_name = 'acked_stream_if'

    def physical_interfaces(self):
        """Two ordinary streams.  In hardware there is no acked stream — there are two FIFOs."""
        return [self.fwd_if, self.ack_if]

    def __post_init__(self) -> None:
        self.endpoint_names = ('master', 'slave')
        if self.clk is None:
            raise ValueError(f"clock must be provided for {type(self).__name__}")
        super().__post_init__()
        self.fwd_if = StreamIF(name=f"{self.name}_fwd", sim=self.sim, clk=self.clk,
                               bitwidth=self.bitwidth, depth=int(self.depth))
        ackd = int(self.ack_depth) if self.ack_depth is not None else int(MAX_IN_FLIGHT)
        self.ack_if = StreamIF(name=f"{self.name}_ack", sim=self.sim, clk=self.clk,
                               bitwidth=self.bitwidth, depth=ackd)

    def bind(self, ep_name: str, endpoint: InterfaceEndpoint) -> None:
        if ep_name not in ('master', 'slave'):
            raise KeyError(
                f"AckedStreamIF has only 'master' and 'slave' sides, but got '{ep_name}'")
        want = AckedStreamMasterIF if ep_name == 'master' else AckedStreamSlaveIF
        if not isinstance(endpoint, want):
            raise TypeError(f"'{ep_name}' side of AckedStreamIF must bind to {want.__name__}")
        if ep_name == 'master':
            self.fwd_if.bind('master', endpoint.fwd_ep)
            self.ack_if.bind('slave', endpoint.ack_ep)
        else:
            self.fwd_if.bind('slave', endpoint.fwd_ep)
            self.ack_if.bind('master', endpoint.ack_ep)
        super().bind(ep_name, endpoint)
        self._check_bound()

    def _check_bound(self) -> None:
        """Enforce the sizing rule rather than hoping for it.

        ``depth >= max_in_flight`` on the ack channel is what makes a dropped status impossible, and
        a dropped status is not a lost verdict — it mis-pairs every later token.  Checked at bind
        because that is the last moment it is cheap and the first moment both numbers exist.
        """
        m = self.endpoints['master']
        if m is None:
            return
        if int(self.ack_if.depth) < int(m.max_in_flight):
            raise ValueError(
                f"AckedStreamIF '{self.name}': ack channel depth {self.ack_if.depth} is shallower "
                f"than max_in_flight={m.max_in_flight}.  A status per accepted frame can then be "
                f"dropped, and a dropped status mis-pairs every later token — silently.  One number "
                f"governs both; raise ack_depth or lower max_in_flight.")
