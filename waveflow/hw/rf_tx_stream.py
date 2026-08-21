"""rf_tx_stream.py — the streaming transmitter: ``TxLoader`` + ``TxPlayer`` over an ``AckedStreamIF``.

Stage 1 of ``plans/rf_samp_new.md``.  The **streaming** replacement for
:mod:`waveflow.hw.rf_samp_buf_tx`'s BRAM design, and the difference is one choice:

===========================  =================================  ==================================
                             ``RfSampBufTx`` (BRAM)             ``RfTxStream`` (this module)
===========================  =================================  ==================================
loader → player              a circular memory                  a **stream** of tagged samples
player → loader              a progress channel (position)      an **ack** (one verdict per window)
how the loader learns        polls a stale lower bound          the player *answers* its command
what a window costs it       ``MARGIN`` samples of horizon      one ``pending`` slot
the loader's inner wait      a data-dependent ``while`` spin    **none**
===========================  =================================  ==================================

**A BRAM was chosen because it cannot refuse a write.**  The price was that the reader has no
back-pressure to learn from, so the position travels out of band, stale, needing ``MARGIN`` to bound
the staleness — and the spin that bounded it is what holds both halves of that design at 2
cycles/word.  Everything above is downstream of *a memory instead of a channel*.

Here the channel is the channel.  The loader's write is **blocking, and that is correct**: on TX the
producer is your own logic, so a stall costs it time and nothing else.  What back-pressure cannot
tell it is whether a sample reached the converter **at the slot it was meant for** — a deadline, not
a resource — and that is what the reverse ack carries.

::

    host ──TxCmd──▶ ┌────────┐ ──AckedStreamIF⟨TaggedSamp⟩──▶ ┌────────┐ ──▶ samp_out (DAC)
    host ──samples─▶ │ loader │ ◀────────── TxStatus ───────── │ player │
         ◀──TxResp── └────────┘                                └────────┘

**Two questions, two paths, deliberately separate.**  *"Was my command accepted?"* is answered by
``TxResp``; *"did it go out on time?"* by the player's ``TxStatus``.  They are joined in the loader —
one ``TxResp`` per command, emitted when the player's verdict for that window comes back — so a
refusal answers immediately and an acceptance answers with the truth instead of a promise.

Where this module departs from the plan's draft
-----------------------------------------------

Three places, each with its reason; see the PR body for the evidence.

1. **The pysim player owns the verdict, and only the verdict.**  The plan says the pysim twin "does
   not model the decision at all" because a player counting underruns would give one phenomenon two
   counters — the edge (``RFSampIF.underrun``) already owns that one.  That argument is exactly right
   *for the underrun* and does not reach ``slot`` or ``verdict``: no edge can know them, because no
   edge has ever seen a ``wr`` tag.  So :class:`TxPlayer` keeps a slot counter and a three-way
   compare, and keeps **no** underrun counter.  Without that split, ``TX_TOO_LATE`` is unreachable in
   pysim and the plan's own Stage 1 assertion 2 cannot be written.

2. **Hand off, then charge — so the player does not use** ``read_frame_nb``.  Stage 0's frame reader
   charges the playout *before returning*, which is right for a consumer whose report is the only
   observable.  It is wrong for one feeding a converter: the existing player records that charging
   before the write makes every block arrive one period late, so the player and the converter
   serialise instead of overlapping.  :class:`TxPlayer` therefore takes frames with the per-item
   reader, writes the block, **then** waits out the block on an absolute grid, and only then sends
   the status.  The property ``read_frame_nb`` exists to protect — *a verdict never precedes the
   playout it describes* — is preserved by that ordering, not given up.

3. **``nsamp == 0`` is refused, with a code of its own** (:data:`TX_ZERO_LEN`).  The plan leaves it
   open and warns what happens if it stays open: a zero-length frame has no last sample, so no mark
   is sent, so no status returns and the ``pending`` slot never pops.  Conflating it with
   ``TX_MISALIGNED`` would report a length fault as a geometry fault, which is the same mistake
   ``TX_TOO_LATE`` was split from ``TOO_OLD`` to avoid.

The absolute grid, and why it is not a detail
---------------------------------------------

:meth:`TxPlayer.run_iter` schedules its next block at ``t0 + nfired * blk_period``, never
``timeout(blk_period)`` at the end of the body.  A relative wait restarts from wherever ``now``
happens to be, so everything the body yielded for is **added** to the period and never given back;
the error is proportional to the firing index, which is fatal for a converter rather than merely
untidy.  The same reasoning, and the same bug, are recorded on
:class:`~waveflow.hw.rf_samp_buf_tx.RfSampBufPlayer` and in ``docs/guide/rf/rfdc/sampling.md``.

It is also what Stage 1's third assertion measures: a design that recovered from a starvation gap by
re-basing its schedule on the gap would still look periodic, and would be wrong.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, ClassVar, NamedTuple

import numpy as np

from waveflow.hw.clock import Clock
from waveflow.hw.dataschema import DataList, IntField
from waveflow.hw.hw_freerun import FreeRunMod
from waveflow.hw.hw_module import HwParam
from waveflow.hw.interface import StreamIF, StreamIFMaster, StreamIFSlave
from waveflow.hw.mem_stream import KernelTask
from waveflow.hw.reverse_stream import (
    MAX_IN_FLIGHT,
    AckedStreamIF,
    AckedStreamMasterIF,
    AckedStreamSlaveIF,
)
from waveflow.hw.rf_samp_buf import IDX_BW, pack_samples, samp_type, sdiff, unpack_samples
from waveflow.hw.synth import sim_only
from waveflow.simulation.simobj import ProcessGen

# ---------------------------------------------------------------------------
# Geometry and vocabulary
# ---------------------------------------------------------------------------

#: One sample is 16 bits.  ``samp_per_word`` changes how many ride one AXIS word at the boundary,
#: never how wide one is — the same separation :data:`~waveflow.hw.rf_samp_buf.IDX_BW` makes for the
#: index.
SAMP_BW = 16

#: Width of the **internal** loader→player channel, in bits.  Not a boundary port, so it is free to
#: be whatever one :class:`TaggedSamp` needs plus Stage 0's mark bit — a 33-bit tagged sample and a
#: 1-bit mark fit one 64-bit beat with room to spare.
TAG_BW = 64

#: ``TxResp.status`` codes.  One vocabulary, and each code is a **distinct repair**:
#: the window played (nothing to do), it arrived after its slot (schedule earlier), its geometry was
#: wrong (fix the length), there was no room to remember it (slow down), or it named no samples at
#: all (a caller bug).  Collapsing any two would report one fault as another.
TX_TRANSMITTED = 0
#: The player reported ``MISSED``: the window reached the converter after its slot had gone out.
TX_TOO_LATE = 1
#: The window was not a whole number of AXIS words.  Refused rather than rounded — a rounded window
#: is data from the wrong time, which nothing downstream can detect.
TX_MISALIGNED = 2
#: The ``pending`` FIFO was full, so the command could not be *remembered*.  An admission condition,
#: not an assertion: accepting it would break the token/verdict correspondence silently.
TX_NO_SLOT = 3
#: ``nsamp == 0``.  See the module docstring — it has no last sample to mark, so it can never
#: resolve, and admitting one leaks a ``pending`` slot for the rest of the run.
TX_ZERO_LEN = 4

#: ``TxStatus.verdict`` — what became of a marked sample when it left the holding register.
PLAYED = 0
MISSED = 1

IdxField = IntField.specialize(bitwidth=IDX_BW, signed=False)
FlagField = IntField.specialize(bitwidth=1, signed=False)
SampField = IntField.specialize(bitwidth=SAMP_BW, signed=False)
VerdictField = IntField.specialize(bitwidth=2, signed=False)


class TxCmd(DataList):
    """One playout command: put ``nsamp`` samples on the slots starting at ``samp_start``.

    Distinct from :class:`waveflow.hw.rf_samp_buf_tx.TxCmd` and deliberately so — that one names a
    *buffer* window and this one names a *schedule*.  They are never used together; the two designs
    are alternatives, not layers.
    """

    include_filename: ClassVar[str | None] = "rf_tx_cmd.h"
    elements = {
        "tid":        {"schema": IdxField, "description": "transaction id, echoed on the response"},
        "samp_start": {"schema": IdxField, "description": "first slot of the window"},
        "start_now":  {"schema": IdxField,
                       "description": "1 = play at the next available slot; the PLAYER assigns it"},
        "nsamp":      {"schema": IdxField, "description": "samples in the window"},
    }


class TxResp(DataList):
    """One response per command — and exactly one, which is what makes it usable without arithmetic.

    ``samp_start`` is **where the window actually went out**, not where it was asked for.  For an
    absolute command those are the same; for ``start_now`` they cannot be, because nothing but the
    player knows where ``slot`` was — so this field is how a host learns "now".
    """

    include_filename: ClassVar[str | None] = "rf_tx_resp.h"
    elements = {
        "tid":        {"schema": IdxField, "description": "the command's transaction id"},
        "status":     {"schema": IdxField, "description": "TX_TRANSMITTED / TOO_LATE / ..."},
        "samp_start": {"schema": IdxField, "description": "the slot the window's first sample took"},
    }


class TxStatus(DataList):
    """What the player reports when a **marked** sample leaves the holding register.

    ``slot`` and ``verdict`` are per-window; ``played_through`` and ``n_underrun`` are cumulative, so
    a lost one is harmless (``plans/rf_samp_new.md`` rule 1).  Nothing in this design differences
    them any more — the per-``tid`` verdict answers the question directly — but they stay because a
    producer that wants a rate picture should not have to add a second channel to get one.
    """

    include_filename: ClassVar[str | None] = "rf_tx_status.h"
    elements = {
        "slot":           {"schema": IdxField, "description": "the slot this status is about"},
        "verdict":        {"schema": VerdictField, "description": "PLAYED | MISSED"},
        "played_through": {"schema": IdxField, "description": "highest slot emitted from real data"},
        "n_underrun":     {"schema": IdxField, "description": "running total of slots filled"},
    }


class TaggedSamp(DataList):
    """One sample, the slot it is for, and whether it must be reported — **the tag travels with the
    sample**.

    A side channel carrying "which sample was for when" is a second stream to keep in step, and
    keeping two streams in step is the defect this deletes.

    ``request_status`` is a field here rather than Stage 0's mark bit, and the reason is that
    **there is no interface object in C++**: ``AckedStreamIF`` lowers to two plain ``hls::stream``
    objects plus the ``pending`` FIFO inside the loader, so nothing but the struct can carry the bit.  Making
    it a field is what lets the two bodies have the same shape — the pysim player reads
    ``t.request_status`` exactly where the C++ one does, instead of reading a mark that only one
    backend has.  Stage 0 still sets its mark on the last item of every frame; here that is a frame
    boundary (the pysim expression of TLAST) and this is the semantic request, and they coincide.
    """

    include_filename: ClassVar[str | None] = "rf_tagged_samp.h"
    elements = {
        "wr":   {"schema": IdxField, "description": "the slot this sample is for (ignored when now)"},
        "now":  {"schema": FlagField, "description": "1 = play at the next available slot"},
        "request_status": {"schema": FlagField,
                           "description": "emit a TxStatus when this sample resolves"},
        "samp": {"schema": SampField, "description": "the sample value"},
    }


#: The schema classes an example must run ``DataSchemaStep`` over.
TX_STREAM_SCHEMA_CLASSES = [TxCmd, TxResp, TxStatus, TaggedSamp]


class Pending(NamedTuple):
    """What the loader remembers about an accepted command until the player answers it.

    Held in the **interface's** pending FIFO, not in a queue of the loader's own — the ordering
    guarantee that makes ``tid`` recovery correct without an id on the wire belongs to
    :class:`~waveflow.hw.reverse_stream.AckedStreamMasterIF`, and hand-rolling it is where the silent
    bug lives.
    """

    tid: int
    slot: int
    nsamp: int
    start_now: int


def tag_frame(samples, slot: int, now: bool) -> np.ndarray:
    """Pack *samples* into one :class:`TaggedSamp` beat each, ready for ``write_frame``.

    Through the generated serializer rather than a shift, for the reason
    ``feedback-use-generated-array-utils`` records: a hand-rolled pack is right at every width until
    it is not, and nothing notices.  ``wr`` is ``slot + i`` and is *ignored* when ``now`` — the
    player assigns those, because the player is the only thing that knows where ``slot`` is.
    """
    vals = np.asarray(samples, dtype=np.uint64).ravel()
    out = np.empty(vals.size, dtype=np.uint64)
    wrap = 1 << IDX_BW
    last = vals.size - 1
    for i, v in enumerate(vals):
        t = TaggedSamp()
        t.wr = int(slot + i) % wrap
        t.now = int(bool(now))
        # THE LAST SAMPLE ANSWERS THE QUESTION.  One request per window is what bounds the reverse
        # rate by construction, which is how this channel satisfies the saturation rule structurally
        # where the RX credit channel satisfies it only by a rate argument.
        t.request_status = int(i == last)
        t.samp = int(v) & ((1 << SAMP_BW) - 1)
        out[i] = int(np.asarray(t.serialize(word_bw=TAG_BW)).ravel()[0])
    return out


def untag(word: int) -> TaggedSamp:
    """The inverse of :func:`tag_frame`, one beat at a time."""
    return TaggedSamp().deserialize(np.array([int(word)], dtype=np.uint64), word_bw=TAG_BW)


# ---------------------------------------------------------------------------
# The loader
# ---------------------------------------------------------------------------


@dataclass
class TxLoader(FreeRunMod):
    """Commands in, tagged samples out, one deferred response per command.

    **No credit, no ``avail()``, no spin** — the write is blocking and that is correct here, so the
    body is a flat sequence with one counted payload loop.  Compare
    :class:`~waveflow.hw.rf_samp_buf_tx.RfSampBufLoader`, whose data-dependent ``while`` around a
    progress channel is what holds it at 2 cycles/word.

    **Refusals answer immediately; acceptances do not.**  A refused command has loaded nothing, so
    there is nothing to wait for.  An accepted one is pushed onto the interface's ``pending`` FIFO
    and answered when the player's verdict comes back — one ``TxResp`` per command either way, and
    the accepted one carries the truth rather than a promise.

    **A refused command must still drain its payload.**  Otherwise the next command's samples are
    read as this one's and every command after it is misaligned — which is why the drain sits
    *outside* the branch in both twins, written as a single ``get`` so the two are obviously the
    same shape.
    """

    #: AXIS word width of the command and sample ports, in bits.
    bitwidth: HwParam[int] = SAMP_BW
    #: Samples carried by one ``samp_in`` word.
    samp_per_word: HwParam[int] = 1
    #: Windows that may be unresolved at once.  One number governs the ``pending`` FIFO **and** the
    #: minimum ack-channel depth, because a marked sample per accepted window means the status FIFO
    #: can never need more.
    max_in_flight: HwParam[int] = MAX_IN_FLIGHT
    #: Statuses read per firing.  **Bounded, and a compile-time constant in the C++ twin**, where it
    #: unrolls into that many ``read_nb`` calls; ``while (got)`` is the data-dependent trip count
    #: that costs the current design its II.
    status_polls: HwParam[int] = MAX_IN_FLIGHT
    clk: Clock = field(default_factory=lambda: Clock(freq=250e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        w, spw = int(self.bitwidth), int(self.samp_per_word)
        if spw & (spw - 1):
            raise ValueError(f"samp_per_word must be a power of two (got {spw})")
        samp_type(w, spw)                        # refuses a sample that would straddle a slot
        self.cmd_in = StreamIFSlave(sim=self.sim, name=f"{self.name}_cmd_in", bitwidth=w,
                                    has_tlast=True)
        self.samp_in = StreamIFSlave(sim=self.sim, name=f"{self.name}_samp_in", bitwidth=w,
                                     has_tlast=True)
        self.resp_out = StreamIFMaster(sim=self.sim, name=f"{self.name}_resp_out", bitwidth=w,
                                       has_tlast=True)
        self.to_player = AckedStreamMasterIF(sim=self.sim, name=f"{self.name}_to_player",
                                             bitwidth=TAG_BW,
                                             max_in_flight=int(self.max_in_flight))
        for ep in (self.cmd_in, self.samp_in, self.resp_out, self.to_player):
            self.add_endpoint(ep)

        #: Commands accepted and remembered.  The denominator for "every command resolved".
        self.n_admitted = 0
        #: Accepted windows the **player** reported ``PLAYED``.
        self.n_transmitted = 0
        #: Accepted windows the player reported ``MISSED`` — reported to the host as
        #: :data:`TX_TOO_LATE`.  Not a second lateness detector: the player is the only one, and this
        #: counts what it said.
        self.n_too_late = 0
        #: Commands refused because the window was not a whole number of words.
        self.n_misaligned = 0
        #: Commands refused because there was no ``pending`` slot free.
        self.n_no_slot = 0
        #: Commands refused because ``nsamp == 0``.
        self.n_zero_len = 0

    def kernel_task(self) -> KernelTask:
        """The hand-written body this module hands over, and the argument order it chose.

        Overriding is the declaration.  Nothing can derive a hand-written task's parameter order —
        it is a fact about the C++ and not about the Python — so it is stated here and
        :meth:`run_iter` stays the pysim golden beside it.
        """
        # `to_player` appears ONCE.  It is one endpoint and two channels, and the composite
        # generator splices both in here, adjacent, in physical_endpoints() order -- which is why
        # the C++ takes (fwd, status) together rather than at positions 3 and 5.
        return KernelTask("rf_tx_loader_task", "rf_tx_loader_task.h",
                          ("cmd_in", "samp_in", "to_player", "resp_out"),
                          template_args=(int(self.bitwidth), TAG_BW, int(self.samp_per_word),
                                         int(self.max_in_flight), int(self.status_polls), IDX_BW))

    # -- counters, marked as instrumentation ----------------------------------------------------

    @sim_only
    def count(self, name: str) -> None:
        """Tally one event.  ``@sim_only`` because a Python counter has no hardware meaning; the C++
        twin keeps its own registers and the two are compared, never shared."""
        setattr(self, name, getattr(self, name) + 1)

    @property
    def n_status_dropped(self) -> int:
        """Statuses lost to a saturated ack FIFO.  **Expected permanently zero.**

        Not "a lost verdict": statuses are paired with pending windows *positionally*, so one drop
        mis-pairs every later ``tid``.  :class:`~waveflow.hw.reverse_stream.AckedStreamIF` refuses at
        bind time to build a channel where it could happen, so a non-zero value here means that check
        was bypassed.
        """
        return int(self.to_player.n_status_dropped)

    def misaligned(self, cmd) -> bool:
        """Is this window a whole number of AXIS words, on a word boundary?

        Both halves matter: a window that *starts* mid-word is as unservable as one that *ends*
        mid-word, and only one of the two is obvious.
        """
        spw = int(self.samp_per_word)
        return bool(int(cmd.nsamp) % spw or int(cmd.samp_start) % spw)

    # -- the body ---------------------------------------------------------------------------------

    def run_iter(self) -> ProcessGen[None]:
        """One pass of the C body's ``while (1)``: harvest, poll for a command, admit, load, drain.

        **The command read is non-blocking, and that is not a style choice.**  The plan's Python
        sketch writes ``cmd = yield from self.cmd_in.get(TxCmd)`` — a *blocking* read — where its own
        C body writes ``if (cmd_in.read_nb(cmd))`` inside ``NO_CMD``.  The two are not equivalent:
        with responses deferred, a host that waits for ``TxResp`` before sending the next command and
        a loader that waits for the next command before harvesting are waiting for each other.  It
        deadlocks on the **first** window, and the symptom is a design that plays exactly one block
        and then goes quiet — which reads like a player fault.  Measured, not reasoned about; see the
        PR body.

        One fabric cycle is charged per empty poll, because that is what the ``while (1)`` costs and
        because ``get_nb`` advances no simulated time on its own — a bare poll loop would be a
        zero-time infinite loop.
        """
        yield from self.harvest()

        cmd = yield from self.cmd_in.get_nb(TxCmd)
        if cmd is None:
            yield self.timeout(self.clk.period)
            return
        nsamp = int(cmd.nsamp)
        slot = int(cmd.samp_start)                 # ignored when start_now: the player assigns
        now = int(cmd.start_now)

        if nsamp == 0:
            refuse = TX_ZERO_LEN
            self.count("n_zero_len")
        elif self.misaligned(cmd):
            refuse = TX_MISALIGNED
            self.count("n_misaligned")
        elif not self.to_player.can_write_frame():          # CHECK, then write
            refuse = TX_NO_SLOT
            self.count("n_no_slot")
        else:
            refuse = None
            self.count("n_admitted")

        if refuse is not None:
            yield from self.respond(int(cmd.tid), refuse, slot)

        # A zero-length command carries no payload frame, so there is nothing to drain and reading
        # would consume the NEXT command's samples -- the very desynchronisation the drain exists to
        # prevent, arrived at from the other side.
        if nsamp == 0:
            return

        npay = (nsamp + int(self.samp_per_word) - 1) // int(self.samp_per_word)
        words = yield from self.samp_in.get(nwords_max=npay)     # DRAIN EITHER WAY
        if refuse is not None:
            return

        samples = unpack_samples(words, int(self.bitwidth), int(self.samp_per_word))[:nsamp]
        yield from self.to_player.write_frame(
            tag_frame(samples, slot, now=bool(now)),
            token=Pending(int(cmd.tid), slot, nsamp, now))

    def harvest(self) -> ProcessGen[int]:
        """Answer whatever the player resolved since last time.  Returns how many.

        **Bounded** (:attr:`status_polls`), never drain-to-empty.  The FIFO and the ordering are the
        interface's; only the slot recovery and the status→``TxResp`` mapping are this module's.
        """
        got = yield from self.to_player.harvest(int(self.status_polls))
        for tok, raw in got:
            st = TxStatus().deserialize(np.array([int(raw)], dtype=np.uint64), word_bw=TAG_BW)
            played = int(st.verdict) == PLAYED
            # A start_now window had no slot until the player assigned one.  Recover it from the
            # status, which reports where the LAST sample of the window actually went out -- this is
            # the whole mechanism by which a host learns "now".
            if tok.start_now and played:
                start = (int(st.slot) - (int(tok.nsamp) - 1)) % (1 << IDX_BW)
            else:
                start = int(tok.slot)
            self.count("n_transmitted" if played else "n_too_late")
            yield from self.respond(int(tok.tid), TX_TRANSMITTED if played else TX_TOO_LATE, start)
        return len(got)

    def respond(self, tid: int, status: int, samp_start: int) -> ProcessGen[None]:
        r = TxResp()
        r.tid, r.status, r.samp_start = int(tid), int(status), int(samp_start) % (1 << IDX_BW)
        yield from self.resp_out.write(r)


# ---------------------------------------------------------------------------
# The player
# ---------------------------------------------------------------------------


@dataclass
class TxPlayer(FreeRunMod):
    """The metronome end: one block of slots per firing, on an absolute grid, never blocking.

    **It does not count underruns, and that is a decision.**  In RTL the player detects one (holding
    register empty at a slot boundary); in pysim the *edge* does
    (:meth:`~waveflow.hw.rf_sample_if.RFSampIF._drain_one` finds its buffer empty on the metronome,
    zero-fills, ``underrun += 1``).  A pysim player that also counted would give one phenomenon two
    counters at two granularities, and they would disagree.  So this one simply **does not deliver a
    block it cannot fill**, and the edge reports what that cost.

    **It does own ``slot`` and ``verdict``.**  No edge can compute those — no edge has seen a ``wr``
    tag — and without them ``TX_TOO_LATE`` is unreachable in pysim.  That is the split: transport
    facts belong to the edge, semantic ones to the consumer, which is the same line
    ``plans/rf_samp_new.md`` draws between credit and ack.

    **Block granularity is a declared limit, not a discovered one.**  The converter's quantum in
    pysim is a block, so a window that is 90% on time reads as *fully* late here and as 10% late in
    RTL.  Same sub-block blindness as ``rf_loopback``'s 72-of-512; it lands on a different counter,
    and stating it first is what keeps it from looking like a gate failure.
    """

    #: AXIS word width of ``samp_out``, in bits.
    bitwidth: HwParam[int] = SAMP_BW
    samp_per_word: HwParam[int] = 1
    #: Samples per block — the pysim handover quantum, matched to the converter's ``blksize``.
    blk_samp: HwParam[int] = 64
    #: Late frames discarded within one block before giving up for that block.  **Bounded**: the C++
    #: twin's ``BEFORE`` case ``continue``\\ s without consuming a slot, and an unbounded version of
    #: that is a data-dependent trip count in a body that must stay II=1.
    max_stale_per_block: HwParam[int] = MAX_IN_FLIGHT
    #: Seconds per **sample** at the converter — the metronome this player is held to.
    #:
    #: A **modelling input, not a hardware parameter**, so it is a plain field rather than an
    #: :class:`~waveflow.hw.hw_module.HwParam`: it must not appear in the elaborated signature or
    #: the generated top, where the metronome arrives as ``TREADY`` instead.  Same split as
    #: :attr:`~waveflow.hw.rf_samp_buf_tx.RfSampBufPlayer.dac_word_rate`.
    #:
    #: ``None`` is legal at construction and **fatal at the first firing**, which is the only place
    #: it means anything.  Refusing it in ``__post_init__`` instead would make the module
    #: unelaboratable — :func:`~waveflow.build.elaborate.elaborate` passes ``HwParam``\s and nothing
    #: else, by design — and elaboration is the sim-free entry point every codegen path starts from.
    #: Measured: it did exactly that.
    #:
    #: Why it matters at all: without the converter's own rate the player runs at the *fabric's*,
    #: which no loader can stay ahead of, and every command comes back ``TX_TOO_LATE`` for a reason
    #: that is in the model rather than in the design.
    slot_period: float | None = None
    clk: Clock = field(default_factory=lambda: Clock(freq=250e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        w, spw = int(self.bitwidth), int(self.samp_per_word)
        if int(self.blk_samp) % spw:
            raise ValueError(f"blk_samp={int(self.blk_samp)} is not a whole number of "
                             f"{spw}-sample words")
        samp_type(w, spw)
        self.fwd = AckedStreamSlaveIF(
            sim=self.sim, name=f"{self.name}_fwd", bitwidth=TAG_BW,
            slot_period=None if self.slot_period is None else float(self.slot_period))
        self.samp_out = StreamIFMaster(sim=self.sim, name=f"{self.name}_samp_out", bitwidth=w,
                                       has_tlast=True)
        for ep in (self.fwd, self.samp_out):
            self.add_endpoint(ep)

        #: The next slot to emit.  Free-running from ``t=0``, exactly as the hardware's is: a DAC
        #: consumes a word every sample period whether or not anything has been loaded.
        self.slot = 0
        #: Highest slot emitted from **real** data.  Does not advance while idle, which is why a
        #: heartbeat built on it would have refreshed nothing (``plans/rf_samp_new.md``).
        self.played_through = 0
        #: Windows discarded as stale.  The player is the **only** lateness detector in the design.
        self.n_missed = 0
        #: Blocks handed to the converter, and blocks in which nothing was due.  Informational, but
        #: the pair is what distinguishes "the host stopped" from "the player stalled".
        self.n_blocks_played = 0
        self.n_blocks_idle = 0
        self._held: list[TaggedSamp] | None = None
        self._held_mark = -1
        self._fired = 0
        self._t0: float | None = None
        #: Optional, ``@sim_only``: the :class:`~waveflow.hw.rf_sample_if.RFSampIF` this player
        #: ultimately feeds, so :meth:`status_word` can report the underrun the **edge** counted.
        #: ONE SOURCE OF TRUTH PER BACKEND — in RTL the number comes from the player, here from the
        #: object that actually owns the sample grid.
        self.tx_edge = None

    def kernel_task(self) -> KernelTask:
        """The hand-written body this module hands over — see :meth:`TxLoader.kernel_task`."""
        return KernelTask("rf_tx_player_task", "rf_tx_player_task.h",
                          ("fwd", "samp_out"),
                          template_args=(int(self.bitwidth), TAG_BW, int(self.samp_per_word),
                                         IDX_BW))

    @property
    def blk_period(self) -> float:
        """Seconds per block — ``blk_samp * slot_period``.  Raises when there is no metronome."""
        if self.slot_period is None:
            raise RuntimeError(
                f"{type(self).__name__} '{self.name}': slot_period was never set, so this player "
                f"has no grid to run on. It is a MODELLING input — construction and elaboration do "
                f"not need it, the first firing does. Pass it from the testbench that owns the "
                f"converter's sample rate.")
        return int(self.blk_samp) * float(self.slot_period)

    @sim_only
    def edge_underrun(self) -> int:
        """What the edge has counted, or 0 when no edge is wired.

        Reading another module's counters has no hardware meaning, which is exactly why this is
        marked rather than inlined: the extractor's implicit-capture rule exists to force that to be
        said out loud instead of baked silently into a body.
        """
        return 0 if self.tx_edge is None else int(self.tx_edge.underrun)

    def status_word(self, slot: int, verdict: int) -> int:
        """One :class:`TxStatus`, serialized to the single beat the ack channel carries."""
        st = TxStatus()
        st.slot = int(slot) % (1 << IDX_BW)
        st.verdict = int(verdict)
        st.played_through = int(self.played_through) % (1 << IDX_BW)
        st.n_underrun = int(self.edge_underrun()) % (1 << IDX_BW)
        return int(np.asarray(st.serialize(word_bw=TAG_BW)).ravel()[0])

    # -- the body ---------------------------------------------------------------------------------

    def run_iter(self) -> ProcessGen[None]:
        """One block of slots.

        Order matters and is the same order the existing player documents: decide, **hand off**,
        wait out the block on the absolute grid, *then* report.  Reporting last is what keeps a
        verdict from reaching the producer before the samples it describes have played — the
        property Stage 0's ``read_frame_nb`` charges for, obtained here by sequencing instead,
        because a converter feeder must write before it waits.
        """
        if self._t0 is None:
            self._t0 = self.now

        base = self.slot
        play: list[TaggedSamp] | None = None
        reports: list[tuple[int, int]] = []          # (slot, verdict), emitted after the block

        # BOUNDED: a stale window is discarded and the next considered within the same block, which
        # is the pysim shape of the C body's `continue` on BEFORE.  `+1` because the last pass may
        # find a window that is on time or early rather than stale.
        for _ in range(int(self.max_stale_per_block) + 1):
            if self._held is None:
                yield from self._take()
            if self._held is None:
                break
            first = self._held[0]
            wr = base if int(first.now) else int(first.wr)
            d = sdiff(wr, base)
            if d == 0:                                    # AT — this block is its block
                play = self._held
                reports.append((base + len(play) - 1, PLAYED))
                self._held = None
                break
            if d > 0:                                     # AFTER — hold it; it is not due yet
                break
            # BEFORE — its slot has gone out.  Discard the whole window and try the next.
            reports.append((base, MISSED))
            self.n_missed += 1
            self._held = None

        if play is not None:
            vals = np.array([int(t.samp) for t in play], dtype=np.uint64)
            self.played_through = (base + len(play) - 1) % (1 << IDX_BW)
            self.n_blocks_played += 1
            # HAND OFF FIRST.  Charging before the write would make every block arrive one period
            # late, so the player and the converter would serialise rather than overlap.
            yield from self.samp_out.write(
                pack_samples(vals, int(self.bitwidth), int(self.samp_per_word)))
        else:
            self.n_blocks_idle += 1

        self.slot = (base + int(self.blk_samp)) % (1 << IDX_BW)
        self._fired += 1
        yield from self._to_grid()

        for slot, verdict in reports:
            yield from self.fwd.send_status(self.status_word(slot, verdict))

    def _take(self) -> ProcessGen[None]:
        """Pull one whole frame off the forward channel, non-blockingly, without charging for it.

        Per-item rather than :meth:`~waveflow.hw.reverse_stream.AckedStreamSlaveIF.read_frame_nb`,
        because that reader charges the playout **before returning** and this player must write
        before it waits (see :meth:`run_iter`).

        The boundary is read from ``request_status`` **in the tag**, not from Stage 0's mark, so this
        loop is the same loop the C++ body runs — where no interface object exists to carry a mark.
        The two bits coincide by construction (:func:`tag_frame` sets the field on the sample
        ``write_frame`` marks), and reading the one both backends have is what keeps the twins
        comparable.
        """
        items = []
        while True:
            r = yield from self.fwd.read_nb()
            if r is None:
                break
            t = untag(r.item)
            items.append(t)
            if int(t.request_status):
                self._held = items
                return
        # A partial frame cannot happen: `write_frame` writes one burst and `read_nb` hands out a
        # whole burst's items, so the mark is always in the same take as its frame's first item.
        if items:
            raise RuntimeError(
                f"{self.name}: took {len(items)} item(s) with no request_status among them. A frame "
                f"is one burst, so the request must arrive in the same take — this means someone "
                f"wrote the forward channel without write_frame()/tag_frame().")

    def _to_grid(self) -> ProcessGen[None]:
        """Wait until this firing's absolute deadline — **never** ``timeout(blk_period)``.

        A relative wait restarts from wherever ``now`` is when the body finishes, so everything the
        body yielded for is added to the period and never given back.  The error grows with the
        firing index, which is what makes it fatal for a converter; and it is exactly what Stage 1's
        third assertion would fail to catch, because a design that re-based its schedule on a gap
        still looks periodic.
        """
        deadline = self._t0 + self._fired * self.blk_period
        yield self.timeout(max(0.0, deadline - self.now))


# ---------------------------------------------------------------------------
# The composite
# ---------------------------------------------------------------------------


@dataclass
class RfTxStream(FreeRunMod):
    """Loader + player + the one :class:`~waveflow.hw.reverse_stream.AckedStreamIF` between them.

    ``fwd_depth`` and the ack depth are the only sizing here, and the ack one is not a choice: it is
    ``max_in_flight``, enforced by the interface at bind.  A shallower ack channel could drop a
    status, and a dropped status mis-pairs every later ``tid`` — silently.
    """

    cpp_kernel_name: ClassVar[str | None] = "rf_tx_stream"

    bitwidth: HwParam[int] = SAMP_BW
    samp_per_word: HwParam[int] = 1
    blk_samp: HwParam[int] = 64
    max_in_flight: HwParam[int] = MAX_IN_FLIGHT
    #: Forward-channel depth in **beats** (one tagged sample each).  Deep enough to hold every window
    #: that may be in flight, because the loader's write is blocking and a shallower channel would
    #: turn a scheduling decision into a stall.
    fwd_depth: int = 0
    slot_period: float | None = None
    clk: Clock = field(default_factory=lambda: Clock(freq=250e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        w, spw = int(self.bitwidth), int(self.samp_per_word)
        mif = int(self.max_in_flight)
        depth = int(self.fwd_depth) or (int(self.blk_samp) * (mif + 1))
        self.loader = TxLoader(sim=self.sim, name=f"{self.name}_loader", bitwidth=w,
                               samp_per_word=spw, max_in_flight=mif, clk=self.clk)
        self.player = TxPlayer(sim=self.sim, name=f"{self.name}_player", bitwidth=w,
                               samp_per_word=spw, blk_samp=int(self.blk_samp),
                               slot_period=self.slot_period, clk=self.clk)
        self.add_comp(self.loader)
        self.add_comp(self.player)

        self.link = AckedStreamIF(name=f"{self.name}_link", sim=self.sim, clk=self.clk,
                                  bitwidth=TAG_BW, depth=depth, ack_depth=mif)
        self.link.bind("master", self.loader.to_player)
        self.link.bind("slave", self.player.fwd)
        self.add_if(self.link)

        self.boundary = ["cmd_in", "samp_in", "resp_out", "samp_out"]
        # Convenience refs: the boundary endpoints live on the children.
        self.cmd_in = self.loader.cmd_in
        self.samp_in = self.loader.samp_in
        self.resp_out = self.loader.resp_out
        self.samp_out = self.player.samp_out

    @property
    def counters(self) -> dict[str, Any]:
        """Every counter this design keeps, in one place — what a gate asserts against."""
        ld, pl = self.loader, self.player
        return {"n_admitted": ld.n_admitted, "n_transmitted": ld.n_transmitted,
                "n_too_late": ld.n_too_late, "n_misaligned": ld.n_misaligned,
                "n_no_slot": ld.n_no_slot, "n_zero_len": ld.n_zero_len,
                "n_status_dropped": ld.n_status_dropped, "n_missed": pl.n_missed,
                "n_blocks_played": pl.n_blocks_played, "n_blocks_idle": pl.n_blocks_idle,
                "played_through": pl.played_through}

    def assert_clean(self) -> None:
        """The two counters that must be zero whatever the scenario.

        ``n_status_dropped`` is a **sizing** claim and ``n_orphan_status`` a **correspondence** one;
        neither is visible in the played data, and both are silent when violated.
        """
        self.loader.to_player.assert_clean()


# ---------------------------------------------------------------------------
# The circular player — the scheduler that used to be a host
# ---------------------------------------------------------------------------


#: Responses harvested per :class:`RfCircPlay` firing.  **Bounded and compile-time in the C++ twin**
#: (rule 3), where it unrolls into that many ``read_nb`` calls.
RESP_POLLS = MAX_IN_FLIGHT

#: Commands :class:`RfCircPlay` keeps outstanding.  **Two, and it is derived rather than tuned.**
#:
#: The verdict for play *k* arrives when its LAST sample plays, at slot ``base + k*PERIOD + nsamp``.
#: Command *k+1* must be loaded before slot ``base + (k+1)*PERIOD``.  So blocking on each response
#: leaves a lead of ``PERIOD - nsamp`` slots: ample when ``PERIOD > nsamp + load time``, and
#: **negative** at back-to-back replay (``PERIOD == nsamp``), where a blocking body underruns by
#: construction.  Two covers both, and it is the configuration ``scratchpad/chain/``'s ``chain_c2``
#: measured as the only non-wedging one.  ``max_in_flight`` bounds it from the other side: the loader
#: refuses with :data:`TX_NO_SLOT` rather than accepting a command it cannot remember.
LEAD = 2


@dataclass
class RfCircPlay(FreeRunMod):
    """Replay a fixed waveform on a fixed period, forever.  **The scheduler, in fabric.**

    Stage 1's testbench problem was that this behaviour is *reactive*: it issues ``start_now``, waits
    for the ``TxResp`` to learn where the waveform landed, and schedules every later play at
    ``samp_start + k*PERIOD``.  A stimulus that depends on the DUT's own output cannot be written
    into a vector file before the run.

    **It moved into the DUT for a design reason, not a tooling one.**  A reactive BFM was perfectly
    possible — ``XsiSimObj`` is a cycle-accurate FSM interface and ``AxiMmReadSlave`` already reacts
    to the DUT — but no practical host could keep up: a scheduler issuing a command every ``period``
    samples at hundreds of MSa/s is fabric work.  Putting it in fabric also avoids a second model,
    which is the dual-modelling trap this arc has paid for repeatedly.

    The testbench's whole job becomes: push ``nsamp`` words once.  That is ``AxisMaster`` with an
    ``in_bundle`` — the BFM that already exists, doing the one thing it is good at.

    **A two-port BRAM would be simpler, and that is the point.**  For this behaviour alone, a modulo
    counter and a memory would do it — no command, no response, no scheduling.  But Example 1 exists
    to exercise the TX path, so it has to *go through* the TX path: ``TxCmd`` in, in-band payload
    behind it, one ``TxResp`` per command, ``start_now`` resolved by the player.  A simpler
    implementation would be a better product and a worse test.

    **The private array is not the BRAM this redesign removed.**  That was a BRAM *shared between two
    tasks*, which has no handshake, which forced the out-of-band progress channel, whose
    data-dependent spin is what Vitis will not pipeline.  This one is local storage inside a single
    task: nothing else reads it, there is no channel, no progress pointer, no ``MARGIN``, no spin.
    """

    bitwidth: HwParam[int] = SAMP_BW
    #: Samples in one play.
    nsamp: HwParam[int] = 64
    #: Slots between successive play **starts**.  ``== nsamp`` is back-to-back replay.
    period: HwParam[int] = 64
    lead: HwParam[int] = LEAD
    resp_polls: HwParam[int] = RESP_POLLS
    clk: Clock = field(default_factory=lambda: Clock(freq=250e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        w = int(self.bitwidth)
        self.wave_in = StreamIFSlave(sim=self.sim, name=f"{self.name}_wave_in", bitwidth=w,
                                     has_tlast=True)
        self.cmd_out = StreamIFMaster(sim=self.sim, name=f"{self.name}_cmd_out", bitwidth=w,
                                      has_tlast=True)
        self.samp_out = StreamIFMaster(sim=self.sim, name=f"{self.name}_samp_out", bitwidth=w,
                                       has_tlast=True)
        self.resp_in = StreamIFSlave(sim=self.sim, name=f"{self.name}_resp_in", bitwidth=w,
                                     has_tlast=True)
        for ep in (self.wave_in, self.cmd_out, self.samp_out, self.resp_in):
            self.add_endpoint(ep)

        self.state = "LOAD"
        self.wave = None
        #: Where play 0 actually landed — learned from the ``start_now`` response and from nothing
        #: else.  Every later slot is arithmetic on this one.
        self.base = 0
        self.k = 0
        self.tid = 0
        self.outstanding = 0
        #: Commands answered ``TX_TRANSMITTED`` / ``TX_TOO_LATE`` / ``TX_NO_SLOT``.
        self.n_played = 0
        self.n_late = 0
        self.n_no_slot = 0
        #: Waveforms accepted, including the first.  A reload returns to ``FIRST``.
        self.n_reloads = 0

    def kernel_task(self) -> KernelTask:
        return KernelTask("rf_circ_play_task", "rf_circ_play_task.h",
                          ("wave_in", "cmd_out", "samp_out", "resp_in"),
                          template_args=(int(self.bitwidth), int(self.nsamp), int(self.period),
                                         int(self.lead), int(self.resp_polls), IDX_BW))

    @sim_only
    def count(self, name: str) -> None:
        setattr(self, name, getattr(self, name) + 1)

    # -- the body ---------------------------------------------------------------------------------

    def run_iter(self) -> ProcessGen[None]:
        """One pass of the C body's ``while (1)``: LOAD, then FIRST, then REPEAT forever."""
        if self.state == "LOAD":
            self.wave = yield from self.wave_in.get(nwords_max=int(self.nsamp))
            self.count("n_reloads")
            self.state = "FIRST"

        elif self.state == "FIRST":
            # start_now: the PLAYER assigns the slots and the response reports where they went.
            # This is the only way to learn "now" -- and the reason there is no zero-length probe
            # command, which would never resolve and would leak a pending slot forever.
            yield from self._issue(start_now=True, samp_start=0)
            r = yield from self.resp_in.get(TxResp)   # blocking HERE is correct: nothing else to do
            # THE TRAIN STARTS AT k = LEAD, NOT k = 1, and that is forced rather than chosen.  The
            # response for the start_now window arrives when its LAST sample has played -- i.e. at
            # slot `base + nsamp`, which at back-to-back replay (period == nsamp) is exactly slot
            # `base + 1*period`.  Play 1 is therefore ALREADY GONE at the moment the scheduler first
            # learns where play 0 landed, and issuing it produces a guaranteed TX_TOO_LATE.
            #
            # Measured before it was fixed: plays alternated played/late forever -- n_played 21,
            # n_late 20 -- because every second command was aimed at a slot that had just passed.
            # `lead` is the right constant because it was derived from the same relationship: it is
            # how many periods of work the scheduler keeps in flight, so it is also how far ahead
            # the first schedulable slot is.
            self.base, self.k, self.state = int(r.samp_start), int(self.lead), "REPEAT"
            self.count("n_played" if int(r.status) == TX_TRANSMITTED else "n_late")

        else:                                          # REPEAT
            # A replacement waveform, checked WITHOUT blocking: a blocking check would stall the
            # schedule waiting for a waveform that may never come.
            new = yield from self.wave_in.get_nb(nwords_max=int(self.nsamp))
            if new is not None:
                # BACK TO FIRST, not REPEAT.  It must re-learn "now" with start_now, because the old
                # `base` describes a schedule the new waveform was never part of.  Continuing the
                # old k sequence would place the first new play at a slot chosen for the old one --
                # which is the "recovery on the original grid" property, applied in the one case
                # where the original grid is the WRONG answer.
                self.wave, self.state = new, "FIRST"
                self.count("n_reloads")
                return
            got = yield from self.poll_resp()
            if self.outstanding < int(self.lead):
                yield from self._issue(start_now=False,
                                       samp_start=self.base + self.k * int(self.period))
                self.outstanding += 1
                self.k += 1
            elif not got:
                # NOTHING HAPPENED THIS PASS: `lead` commands are already out and no response has
                # come back.  The C body's `while (1)` spins on real cycles here; `get_nb` advances
                # no simulated time, so a pysim twin that just returned would be a ZERO-TIME
                # INFINITE LOOP -- SimPy would re-enter forever at one timestamp and the run would
                # never end.  Measured, not reasoned about: it hung.
                #
                # One fabric cycle is the honest charge, and it is what the loader's NO_CMD poll
                # already pays for the same reason.
                yield self.timeout(self.clk.period)

    def poll_resp(self) -> ProcessGen[int]:
        """Up to :attr:`resp_polls` responses.  **BOUNDED** — never drain-to-empty (rule 3).

        Deliberately NOT called ``harvest``.  :meth:`TxLoader.harvest` pops the *pending FIFO* and
        emits a ``TxResp`` — it maintains a correspondence.  This has no FIFO and no correspondence:
        it counts responses and frees slots.  Same shape, different job, and one name for both would
        send a reader looking for a FIFO that is not there.
        """
        got = 0
        for _ in range(int(self.resp_polls)):
            r = yield from self.resp_in.get_nb(TxResp)
            if r is None:
                break
            got += 1
            # The C twin decrements without a guard, because the loader guarantees one response per
            # accepted command; if it underflowed, that guarantee had already broken and a counter is
            # the wrong place to find out.  pysim is where an invariant is cheap to check, so it is
            # checked here.
            assert self.outstanding > 0, (
                f"{self.name}: a TxResp arrived with nothing outstanding — the loader's "
                f"one-response-per-accepted-command guarantee has broken")
            self.outstanding -= 1
            st = int(r.status)
            self.count("n_played" if st == TX_TRANSMITTED
                       else ("n_no_slot" if st == TX_NO_SLOT else "n_late"))
        return got

    def _issue(self, *, start_now: bool, samp_start: int) -> ProcessGen[None]:
        """One ``TxCmd``, then its payload in-band behind it.  Both writes blocking, and correct:
        on TX a stall costs the producer time and loses nothing."""
        c = TxCmd()
        c.tid = int(self.tid) & 0xFFFF
        c.samp_start = int(samp_start) % (1 << IDX_BW)
        c.start_now = int(bool(start_now))
        c.nsamp = int(self.nsamp)
        yield from self.cmd_out.write(c)
        yield from self.samp_out.write(np.asarray(self.wave, dtype=np.uint64))
        self.tid += 1

    @property
    def counters(self) -> dict[str, Any]:
        return {"n_played": self.n_played, "n_late": self.n_late, "n_no_slot": self.n_no_slot,
                "n_reloads": self.n_reloads, "outstanding": self.outstanding, "base": self.base,
                "k": self.k}


@dataclass
class RfRepeatPlay(FreeRunMod):
    """:class:`RfCircPlay` + :class:`RfTxStream` — the whole transmitter, scheduler included.

    Two boundary ports: a waveform in, samples out.  Everything between them is the design.
    """

    cpp_kernel_name: ClassVar[str | None] = "rf_repeat_play"

    bitwidth: HwParam[int] = SAMP_BW
    samp_per_word: HwParam[int] = 1
    nsamp: HwParam[int] = 64
    period: HwParam[int] = 64
    blk_samp: HwParam[int] = 64
    max_in_flight: HwParam[int] = MAX_IN_FLIGHT
    lead: HwParam[int] = LEAD
    fwd_depth: int = 0
    slot_period: float | None = None
    clk: Clock = field(default_factory=lambda: Clock(freq=250e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        w = int(self.bitwidth)
        self.sched = RfCircPlay(sim=self.sim, name=f"{self.name}_sched", bitwidth=w,
                                nsamp=int(self.nsamp), period=int(self.period),
                                lead=int(self.lead), clk=self.clk)
        self.tx = RfTxStream(sim=self.sim, name=f"{self.name}_tx", bitwidth=w,
                             samp_per_word=int(self.samp_per_word), blk_samp=int(self.blk_samp),
                             max_in_flight=int(self.max_in_flight), fwd_depth=int(self.fwd_depth),
                             slot_period=self.slot_period, clk=self.clk)
        self.add_comp(self.sched)
        self.add_comp(self.tx)

        # The three channels that used to be testbench wires.  `cmd` and `samp` are deep enough for
        # `lead` commands in flight; `resp` for the responses those produce.
        depth = int(self.nsamp) * (int(self.lead) + 1)
        for nm, master, slave, d in (
                ("cmd", self.sched.cmd_out, self.tx.cmd_in, 4 * (int(self.lead) + 1)),
                ("samp", self.sched.samp_out, self.tx.samp_in, depth),
                ("resp", self.tx.resp_out, self.sched.resp_in, 4 * (int(self.lead) + 1))):
            ifc = StreamIF(name=f"{self.name}_{nm}", sim=self.sim, clk=self.clk, bitwidth=w,
                           depth=d)
            ifc.bind("master", master)
            ifc.bind("slave", slave)
            self.add_if(ifc)
            setattr(self, f"{nm}_if", ifc)

        self.boundary = ["wave_in", "samp_out"]
        self.wave_in = self.sched.wave_in
        self.samp_out = self.tx.samp_out

    @property
    def counters(self) -> dict[str, Any]:
        return {**self.tx.counters, **{f"sched_{k}": v for k, v in self.sched.counters.items()}}
