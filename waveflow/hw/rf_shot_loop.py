r"""rf_shot_loop.py — **infinite play**: change the waveform without stopping the converter.

``plans/t2p_lock_chan.md`` S1, and the first consumer of
:class:`~waveflow.hw.locked_mem.LockedT2pMemIF`.  :class:`~waveflow.hw.rf_shot_tx.RfShotTx` plays a
shot ``nrepeat`` times and goes quiet, and a load arriving mid-play is refused with
:data:`~waveflow.hw.rf_shot_tx.SHOT_BUSY` — because the memory is under a live reader and there is no
way to say *stop touching it*.  The lab flow is **load a waveform, run it a long while, then change
it**, so infinite play plus a clean handover is the missing capability.

Three tasks and one memory beside them::

    s_in --[ShotTxHdr | samples ... TLAST]--> ShotLoopLoad --[ ACQUIRE ]--> [ BRAM ]
    resp_out <------------- ShotTxResp -----------  |                          |
                                                    +--[ RELEASE ]--> ShotLoopPlay (the OWNER)
                                                                              |
                                                          dense --> RfRelayoutToSlots --> converter

**Why this is a sibling of** :class:`~waveflow.hw.rf_shot_tx.RfShotTx` **rather than a mode of it.**
The two designs differ in what the player *is*: a finite player consumes a token, plays, and reports
done, so the loader owning the memory is safe by construction; an infinite player never stops, so the
memory has to be handed over.  That is a different task, a different loader and a different channel
set — and the numbers ``examples/rf_shot_play`` measured belong to the design it measured.  Keeping
them separate is what makes the comparison in ``docs/guide/rf/choosing.md`` checkable rather than
asserted, exactly as ``rf_shot_play`` and ``rf_repeat_play`` already are.

What is gone, and what it cost
------------------------------
There is **no** ``rdy`` token, **no** ``rep`` channel, **no** ``done`` token and no
:class:`~waveflow.hw.rf_shot_buf.RfShotBufLoad` / ``RfShotBufRead`` pair: the loader writes the memory
itself and the player reads it itself, which is what having a lock buys.  Five tasks become three.
What replaces them is two lock streams and one rule — **set the state before you grant** — and that
rule is the only thing in this module that is hard.

**The re-layout is last, and that moved a modelling field with it.**  The memory holds *dense* words
(the logic-side format a host can write without knowing anything about justification), so the
conversion to converter slots has to happen after the player.  pysim's quantum on the converter edge
is a *block*, so whichever stage is last carries :attr:`~waveflow.hw.rf_relayout._RelayoutTask.blk_words`
— the accommodation follows the port, not the class.

**One region, and that is the honest limit.**  The player yields the whole shot region and plays
filler until the release, so a change of waveform is a **gap**, not a crossfade.  On TX that is
acceptable — you already accepted discontinuity.  On RX it would be dropped samples, which is why
``plans/t2p_lock_chan.md`` puts the second region in S2 and calls it correctness rather than an
optimisation.

**A short load clobbers.**  With one region there is nowhere else to put an arriving shot, so a
transfer that ends early overwrites the waveform that was playing and the design plays the padded
result.  :data:`~waveflow.hw.rf_shot_tx.SHOT_SHORT` still goes back to the host, which is the whole
warning there is; ``RfShotTx`` can do better only because it has a phase in which the memory is idle.
This is stated rather than hidden because it is the first thing S2's second region fixes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar

import numpy as np

from waveflow.hw.bram import T2pBram, word_element
from waveflow.hw.clock import Clock
from waveflow.hw.codegen_targets import COMPOSITE_KERNEL
from waveflow.hw.hw_freerun import FreeRunMod
from waveflow.hw.hw_module import HwParam
from waveflow.hw.interface import (
    FramedStreamIFMaster,
    FramedStreamIFSlave,
    StreamIF,
    StreamIFMaster,
)
from waveflow.hw.locked_mem import (
    LOCK_ACQUIRE,
    LOCK_GRANTED,
    LOCK_STATUS_NAMES,
    LockedMemMasterIF,
    LockedMemSlaveIF,
    LockedT2pMemIF,
)
from waveflow.hw.mem_stream import KernelTask
from waveflow.hw.rf_relayout import RfRelayoutToSlots
from waveflow.hw.rf_samp_buf import IDX_BW
from waveflow.hw.rf_shot_buf import BUF_DEPTH, SHOT_WORDS, WORD_BW
from waveflow.hw.rf_shot_tx import (
    SHOT_END,
    SHOT_LOADED,
    SHOT_LOOP,
    SHOT_SHORT,
    SHOT_WRONG_LEN,
    SHOT_ZERO_LEN,
    ShotTxHdr,
    ShotTxResp,
)
from waveflow.simulation.simobj import ProcessGen

#: What the player emits while it does not own the memory.  **Zero, and it is a value rather than a
#: stall**: the owner cannot stop, so a body that blocked here would back-pressure the converter —
#: which is not an option and is the whole reason this side is the owner.  Zero is also the one
#: sample a DAC can be handed that means *nothing*, so the gap is silence rather than noise.
FILLER = 0


# ---------------------------------------------------------------------------
# The loader — the REQUESTER
# ---------------------------------------------------------------------------

@dataclass
class ShotLoopLoad(FreeRunMod):
    r"""Read a frame, decide, take the region, write it, give it back, answer.

    The requester side of the lock: it holds nothing, arrives with a transaction, and is the *bursty*
    half.  Compare :class:`~waveflow.hw.rf_shot_tx.ShotTxLoad`, which does the same command work and
    then hands the payload to a separate buffer task — here there is no separate task, because the
    lock is what makes writing the memory directly safe.

    **Three verdicts, not five.**  :data:`~waveflow.hw.rf_shot_tx.SHOT_ZERO_LEN` and
    :data:`~waveflow.hw.rf_shot_tx.SHOT_WRONG_LEN` are faults in the command, decided from the header
    alone; :data:`~waveflow.hw.rf_shot_tx.SHOT_SHORT` is what the stream turned out to be.
    :data:`~waveflow.hw.rf_shot_tx.SHOT_BUSY` **is unreachable and that is the point of the whole
    module** — under infinite play the player never stops, so a design that answered ``BUSY`` would
    refuse every load forever.  Handing the memory over is what replaces it.

    **The opcode must be** :data:`~waveflow.hw.rf_shot_tx.SHOT_LOOP`.  A ``SHOT_LOAD`` asks for
    ``nrepeat`` plays and then quiet, which this design cannot provide, so it is refused as
    :data:`~waveflow.hw.rf_shot_tx.SHOT_WRONG_LEN`'s sibling rather than silently reinterpreted — a
    command answered as something other than what it asked for is the failure a verdict exists to
    prevent.
    """

    cpp_kernel_name: ClassVar[str | None] = "shot_loop_load"

    #: Word width in bits — the host port's and the memory's.
    bitwidth: HwParam[int] = WORD_BW
    #: Memory depth in **elements**; the bound every region is checked against.
    depth: HwParam[int] = BUF_DEPTH
    #: Words in one shot.  Build-time structure and the single source for the length: the header's
    #: ``nsamp`` is what the *host* believes, and catching the two disagreeing is what
    #: :data:`~waveflow.hw.rf_shot_tx.SHOT_WRONG_LEN` is.
    nword: HwParam[int] = SHOT_WORDS
    #: Samples one word carries — what turns a word count into the ``nsamp`` a host speaks in.
    samp_per_word: HwParam[int] = 4
    #: First element of the region.  **Non-zero is the interesting case**: ``base + offset`` is the
    #: shape of the byte-versus-word bug ``bram_toy`` stayed green through, because consistently
    #: mis-scaled addressing round-trips perfectly right up to the top of the address space.
    base: HwParam[int] = 0
    clk: Clock = field(default_factory=lambda: Clock(freq=250e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        w, d, nw = int(self.bitwidth), int(self.depth), int(self.nword)
        spw, b = int(self.samp_per_word), int(self.base)
        if nw < 1:
            raise ValueError(f"a shot is {nw} words; there is nothing to load")
        if spw < 1 or w % spw:
            raise ValueError(
                f"a {w}-bit word cannot carry {spw} samples without one straddling a slot")
        if b < 0 or b + nw > d:
            raise ValueError(
                f"the region [{b}, {b + nw}) does not fit a {d}-element memory. Refused here rather "
                f"than on the wire, where it would come back as LOCK_BAD_RANGE every single load.")
        if nw * spw >= (1 << IDX_BW):
            raise ValueError(
                f"a shot of {nw} words x {spw} samples is {nw * spw} samples, which does not fit "
                f"the {IDX_BW}-bit nsamp field. A verdict that wrapped would report a short load as "
                f"a correct one.")
        #: The host's port: header **and** payload, one frame, ``TLAST`` at the end.  Without the
        #: pin there is no in-band way to say *that was the end* — a payload word and a header word
        #: are the same bits — so a short transfer would stall a counted loop, and a hang is
        #: indistinguishable from a deadlock.
        self.s_in = FramedStreamIFSlave(sim=self.sim, name=f"{self.name}_s_in", bitwidth=w,
                                        has_tlast=True)
        #: The verdict, with its own ``TLAST`` so a host reading through a DMA S2MM channel learns
        #: where one response ends without being told how long it is.
        self.resp_out = FramedStreamIFMaster(sim=self.sim, name=f"{self.name}_resp", bitwidth=w,
                                             has_tlast=True)
        #: One endpoint, three channels: the memory port, the command out, the response in.
        self.lock = LockedMemMasterIF(sim=self.sim, name=f"{self.name}_lock",
                                      element_type=word_element(w), nelem=d, access="write")
        for ep in (self.s_in, self.resp_out, self.lock):
            self.add_endpoint(ep)
        #: ``(tid, status, nsamp_loaded)`` for every response, in the order they went out.
        self.resps: list[tuple[int, int, int]] = []
        #: Shots actually written to the memory.  Counted rather than inferred from the verdicts: a
        #: run whose loader answered correctly and stored nothing passes every response check.
        self.n_stored = 0

    # -- geometry ----------------------------------------------------------------------------

    @property
    def nsamp_shot(self) -> int:
        """Samples in a full shot — the one value ``ShotTxHdr.nsamp`` may carry."""
        return int(self.nword) * int(self.samp_per_word)

    @property
    def region(self) -> tuple[int, int]:
        """``[base, base + nword)`` — the one region this design ever asks for."""
        return int(self.base), int(self.base) + int(self.nword)

    def kernel_task(self) -> KernelTask:
        # `lock` appears ONCE and becomes THREE arguments, spliced in adjacent in
        # physical_endpoints() order -- which is why the C++ takes (buf, cmd, resp) together.
        return KernelTask("shot_loop_load_task", "shot_loop_load_task.h",
                          ("s_in", "lock", "resp_out"),
                          template_args=(int(self.bitwidth), int(self.depth), int(self.nword),
                                         int(self.samp_per_word), int(self.base)))

    # -- the pysim twin ----------------------------------------------------------------------

    def _verdict(self, hdr) -> int:
        """The header-only refusals, in the order the C++ body tests them.

        **Malformed before transient**, which is the repo's order and for its reason: a command that
        is wrong *and* badly timed should be told the thing it can fix.  Here nothing is transient —
        there is no ``BUSY`` — so the order is only the two malformed cases and the opcode.
        """
        if int(hdr.opcode) != SHOT_LOOP:
            return SHOT_WRONG_LEN
        if int(hdr.nsamp) == 0:
            return SHOT_ZERO_LEN
        if int(hdr.nsamp) != self.nsamp_shot:
            return SHOT_WRONG_LEN
        return SHOT_LOADED

    def run_iter(self) -> ProcessGen[None]:
        """One firing is one frame, which is one iteration of the C++ body.

        ``get()`` with no count returns **the whole burst**, and a burst is a ``TLAST``-delimited
        frame — so the read of "the header, then words until last" is one call and the two backends
        see the same boundary rather than two encodings of it.  The plan's sketch splits it into
        ``get_schema`` + ``get_pipelined``; that would be two ``get``\\ s against one frame, and a
        pysim slave dequeues a whole burst per ``get``, so the first would swallow the payload.

        **A known twin divergence follows from that, and it is recorded rather than hidden.**  The
        C++ body acquires the region and *then* reads the payload beat by beat, because
        ``buf[lo + i] = s_in.read()`` at II=1 is one pipeline and there is no way to have it without
        owning the region first.  This body cannot: the frame is already in hand before there is
        anything to decide.  So the RTL holds the region for the whole **transfer** and pysim holds
        it for the **write**, and the handover gap the two backends produce is a different length.
        Both are measured; neither number is inherited from the other.
        """
        w, nw = int(self.bitwidth), int(self.nword)
        frame = np.asarray((yield from self.s_in.get()), dtype=np.uint64).ravel()

        hn = ShotTxHdr.nwords_per_inst(w)
        # The schema's own deserializer, never a field walk: the layout has one author and the
        # generated C++ reads it through the same statement.
        hdr = ShotTxHdr().deserialize(frame[:hn], word_bw=w)
        payload = frame[hn:]

        if int(hdr.opcode) == SHOT_END:
            # A fence.  An hls::task has no loop to break, so what END is worth is what its RESPONSE
            # proves: headers are answered strictly in order, so this one says everything ahead of it
            # has been processed.  A testbench that ended by timing out could not tell a finished run
            # from a deadlocked one.
            yield from self._answer(hdr, SHOT_LOADED, 0)
            return

        status = self._verdict(hdr)
        if status != SHOT_LOADED:
            # Nothing to drain: the frame arrived whole, so a refused command leaves no residue on
            # the wire to become the next header.  That is the framed port's doing, not this body's.
            yield from self._answer(hdr, status, 0)
            return

        took = min(int(payload.size), nw)
        # A short frame is completed with ZEROS.  It clobbers -- see the module docstring: with one
        # region there is nowhere else to put an arriving shot, and the verdict is the warning.
        words = np.zeros(nw, dtype=np.uint64)
        words[:took] = payload[:took]

        lo, hi = self.region
        lock_status = yield from self.lock.acquire(lo, hi)
        if lock_status != LOCK_GRANTED:
            # Unreachable while `base` and `nword` are checked at construction, which is where the
            # same predicate already ran.  Raised rather than answered, because a region the design
            # declared and the memory refuses is a wiring fault, not a host's mistake.
            raise AssertionError(
                f"{type(self).__name__} '{self.name}': ACQUIRE [{lo}, {hi}) came back "
                f"{LOCK_STATUS_NAMES.get(lock_status, lock_status)} on a memory of "
                f"{int(self.depth)} elements. The region is build-time structure and was checked at "
                f"construction, so the two ends disagree about the geometry.")
        yield from self.lock.write_pipelined(words, addr=lo)
        # A BARRIER, not a hint: the owner may resume the instant it sees this.
        yield from self.lock.release()
        self.n_stored += 1

        if took < nw:
            status = SHOT_SHORT                 # THE verdict this response exists for
        yield from self._answer(hdr, status, took * int(self.samp_per_word))

    def _answer(self, hdr, status: int, nsamp_loaded: int) -> ProcessGen[None]:
        """Exactly one :class:`~waveflow.hw.rf_shot_tx.ShotTxResp` per header."""
        r = ShotTxResp()
        r.tid, r.status, r.nsamp_loaded = int(hdr.tid), int(status), int(nsamp_loaded)
        self.resps.append((int(hdr.tid), int(status), int(nsamp_loaded)))
        yield from self.resp_out.write(r)


# ---------------------------------------------------------------------------
# The player — the OWNER
# ---------------------------------------------------------------------------

@dataclass
class ShotLoopPlay(FreeRunMod):
    r"""Play the region forever, yield it on request, play filler until it comes back.

    The owner side of the lock: it holds the whole memory by default, it cannot stop, and it polls
    the command channel exactly once per :attr:`blk_words` elements of its own work — which is what
    makes the loader's blocking wait for a grant a **stated number** rather than a hope.

    **The one ordering everything turns on**::

        self.playing = False            # STOP TOUCHING IT ...
        yield from self.lock.grant(...) # ... THEN grant

    Granting while still reading lets the loader write memory this task is reading — precisely the
    collision the interface exists to prevent.  :meth:`~waveflow.hw.locked_mem.LockedMemSlaveIF.grant`
    takes the region out of the owner's hands before the answer goes on the wire, so getting the two
    lines the wrong way round **raises on the very next chunk** instead of returning a plausible
    sample.

    **No schedule, and no deadline.**  The converter back-pressures, the re-layout back-pressures,
    this task stalls, and the memory holds.  Once a play has started, a BRAM read at II=1 can always
    supply a word per cycle, so the only reachable underruns are before the first word — which is
    what the filler covers.

    **A ``static`` per firing, and therefore the reset trap.**  ``rd`` and ``playing`` are carried
    across firings, and this body **writes before it reads** — writing without being asked is what
    *the side that cannot stop* means.  That is exactly ``reference-hls-task-reset-trap``, so the C++
    twin carries ``#pragma HLS reset`` on both **and** the build needs ``config_rtl -reset state``,
    which is what actually closed it under Vitis 2025.1.  The requester next door opens with a
    blocking read and inherits none of this.
    """

    cpp_kernel_name: ClassVar[str | None] = "shot_loop_play"

    bitwidth: HwParam[int] = WORD_BW
    depth: HwParam[int] = BUF_DEPTH
    #: Words in one shot — the region's length, and the wrap point of the read pointer.
    nword: HwParam[int] = SHOT_WORDS
    #: First element of the region.  Must match the loader's; the composite passes one number.
    base: HwParam[int] = 0
    #: Elements between polls, **and** words per pysim output burst — one number, because they are
    #: the same boundary.  A poll per converter block is the natural cadence: the grant latency a
    #: loader sees is then one block, which is the shortest gap the design could offer anyway.
    #:
    #: An ``HwParam`` unlike :attr:`~waveflow.hw.rf_shot_tx.ShotTxPlay.blk_words`, because here it
    #: **is** hardware: it is the trip count of the pipelined loop the poll sits outside of.
    blk_words: HwParam[int] = 1

    #: **Words per second the DAC consumes** — ``samp_rate / samp_per_word`` — or ``None`` to run at
    #: the fabric's rate alone.  A modelling input and not hardware: in RTL this task is paced by
    #: ``TREADY`` and needs nothing, but pysim does not back-pressure a burst write, so the metronome
    #: has to be handed over.  Left unset the playout runs at the fabric's rate, the converter is
    #: never the bottleneck, and the one property this design claims is not being tested.
    dac_word_rate: float | None = None
    clk: Clock = field(default_factory=lambda: Clock(freq=250e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        w, d, nw = int(self.bitwidth), int(self.depth), int(self.nword)
        b, bw = int(self.base), int(self.blk_words)
        if b < 0 or b + nw > d:
            raise ValueError(f"the region [{b}, {b + nw}) does not fit a {d}-element memory")
        if bw < 1 or nw % bw:
            raise ValueError(
                f"blk_words={bw} does not divide a {nw}-word shot. A chunk that straddled the end of "
                f"the region would need two base additions, and the play boundary would stop landing "
                f"on a block boundary.")
        self.lock = LockedMemSlaveIF(sim=self.sim, name=f"{self.name}_lock",
                                     element_type=word_element(w), nelem=d, access="read",
                                     check_period=bw)
        self.samp_out = StreamIFMaster(sim=self.sim, name=f"{self.name}_samp", bitwidth=w,
                                       has_tlast=True)
        for ep in (self.lock, self.samp_out):
            self.add_endpoint(ep)

        #: The read pointer **within the region** — a ``static`` in the C++ twin.
        self.rd = 0
        #: ``True`` while it owns the region.  **Starts false**: nothing has been loaded yet, and
        #: playing a memory that was never written is a plausible sample rather than a silence.
        self.playing = False
        #: Chunks emitted, chunks that were filler, and words that came out of the memory.  All
        #: three, because a run that played nothing but filler looks identical to a run that played
        #: nothing at all, and a truncated play carries the right samples as far as it got.
        self.n_chunks = 0
        self.n_filler = 0
        self.n_words = 0
        #: Times the waveform came back after a handover — one per release seen.
        self.n_resumed = 0
        #: The pysim rate grid: when the first block went out, and how many have.
        self._t0: float | None = None
        self._blocks = 0

    def kernel_task(self) -> KernelTask:
        return KernelTask("shot_loop_play_task", "shot_loop_play_task.h",
                          ("lock", "samp_out"),
                          template_args=(int(self.bitwidth), int(self.depth), int(self.nword),
                                         int(self.base), int(self.blk_words)))

    def run_iter(self) -> ProcessGen[None]:
        """One firing is one chunk **and exactly one poll** — the C++ body's outer iteration.

        The rate is charged **per block on an absolute grid**, never as a relative timeout.  A
        relative wait restarts from wherever ``now`` happens to be when the body finishes, so
        everything the body yielded for is added to the period and never given back — the defect
        that made ``rf_samp_buf_tx``'s player slip a whole block every fourth firing.
        """
        yield from self._chunk_and_pace()

        # EXACTLY ONE POLL, and it is outside everything above -- that is what `check_period` means
        # and what keeps the datapath's II untouched.  `handle_nb` applies a RELEASE on the spot
        # because a release needs no decision and no answer; an ACQUIRE comes back untouched
        # precisely because granting it is this body's call and must follow the state change.
        cmd = yield from self.lock.handle_nb()
        if cmd is None:
            return
        if int(cmd.opcode) == LOCK_ACQUIRE:
            self.playing = False                    # STOP TOUCHING IT ...
            yield from self.lock.grant(int(cmd.start_addr), int(cmd.end_addr))   # ... THEN grant
        else:
            # A new waveform starts at its beginning: resuming mid-shot would splice the tail of the
            # old waveform's phase onto the new one, which is right in no application and is
            # invisible from a word count.
            self.rd = 0
            self.playing = True
            self.n_resumed += 1

    def _chunk_and_pace(self) -> ProcessGen[None]:
        """One chunk out — from the memory when it owns it, filler when it does not — then the rate.

        Split from :meth:`run_iter` so the datapath and the *decision* are separable: the poll is one
        firing's worth of lock traffic sitting outside a body that is otherwise a counted loop, which
        is exactly the shape the C++ has and exactly why ``II=1`` survives having a lock at all.

        The rate is charged **after** the hand-off, never before: charging first would make every
        block arrive one period late, so the player and the converter would serialize rather than
        overlap.
        """
        w, bw = int(self.bitwidth), int(self.blk_words)
        if self.playing:
            data, t0 = yield from self.lock.read_pipelined(word_element(w), bw,
                                                           addr=int(self.base) + self.rd)
            yield from self.samp_out.write_pipelined(data, t_out_start=t0)
            self.n_words += bw
            self.rd = (self.rd + bw) % int(self.nword)
        else:
            yield from self.samp_out.write(np.full(bw, FILLER, dtype=np.uint64))
            self.n_filler += 1
        self.n_chunks += 1

        if self._t0 is None:
            # The grid's origin: the instant the FIRST block went out.  Set here rather than at
            # construction, because anchoring at t=0 would charge the design for time before it had
            # anything to put on the wire.
            self._t0 = self.now
        self._blocks += 1
        if self.dac_word_rate:
            deadline = self._t0 + self._blocks * (bw / float(self.dac_word_rate))
            yield self.timeout(max(0.0, deadline - self.now))


# ---------------------------------------------------------------------------
# The composite
# ---------------------------------------------------------------------------

@dataclass
class RfShotTxLoop(FreeRunMod):
    r"""The whole infinite-play transmitter as one design scope.

    Three ``hls::task``\ s and one memory beside them::

        s_in --> ShotLoopLoad --[lock]--> [ BRAM ] --[lock]--> ShotLoopPlay --> RfRelayoutToSlots
                     |                                                                   |
                 resp_out                                                            samp_out

    The registrations *are* the design, and the interesting one is a single line:

    ============================  ==============================================================
    ``add_comp(load/play/relay)`` the three ``hls::task``\ s inside the generated kernel
    ``add_rtl_mod(mem)``          the memory, hand-written Verilog **beside** the top
    ``add_if(lock)``              **two** registries — the lock streams become internal FIFOs and
                                  the two ``BramIF``\ s are swept into the RTL registry, so the
                                  tasks' memory ports stay BOUNDARY ports
    ============================  ==============================================================

    Compare :class:`~waveflow.hw.rf_shot_tx.RfShotTx`, which needs six wiring blocks and five tasks
    to move the same samples: seven internal channels, two hand-wired ``BramIF``\ s, a shared
    :class:`~waveflow.hw.rf_shot_buf.ShotPhase` object, and a ``done`` token whose only job is to say
    *may I overwrite the memory yet*.  All of that is the question the lock answers directly.
    """

    cpp_kernel_name: ClassVar[str | None] = "rf_shot_tx_loop"
    potential_targets: ClassVar[frozenset[str]] = frozenset({COMPOSITE_KERNEL})

    #: The player class this composite instantiates.  **A ClassVar, so it is changed by subclassing
    #: and never by a caller** — which one a design has is structure, not configuration.
    #:
    #: It exists for exactly one reason, and it is a verification one.  The RTL check for the
    #: collision this lock prevents is a VCD scan, and a scan that finds nothing is
    #: indistinguishable from a scan bound to the wrong nets unless it is **paired with a run known
    #: to collide** (``reference-xsi-discards-rtl-text``).  That dirty twin has to be *this* design
    #: with one line changed — a separately written broken design would prove nothing about the
    #: shipped one — and this is the seam that lets an example build it without copying the
    #: composite.  See ``examples/rf_shot_loop``'s ``RfShotTxLoopDirty``.
    player_cls: ClassVar[type] = ShotLoopPlay

    #: Word width in bits — one number for the host port, the memory, the player and the converter.
    bitwidth: HwParam[int] = WORD_BW
    #: Samples one word carries.
    samp_per_word: HwParam[int] = 4
    #: Memory depth in **WORDS** (a power of two: the address wrap is a mask).
    depth: HwParam[int] = BUF_DEPTH
    #: Words in one shot.  ``base + nword <= depth``.
    nword: HwParam[int] = SHOT_WORDS
    #: First element of the region — see :attr:`ShotLoopLoad.base`.  **One number, passed to both
    #: ends**, because a loader and a player that disagreed about where the waveform is would each
    #: be individually correct.
    base: HwParam[int] = 0
    #: Bits the effective sample sits above the bottom of its converter slot.  **0 makes the last
    #: stage the identity**, so a build that leaves it there is measuring a pair of wires.
    shift: HwParam[int] = 2
    #: Words per converter block: the player's chunk, its poll period, and the re-layout's pysim
    #: burst.  One number for all three because they are one boundary.
    blk_words: HwParam[int] = 1
    #: The DAC's word rate — :attr:`ShotLoopPlay.dac_word_rate`, passed through.  Not hardware.
    dac_word_rate: float | None = None
    clk: Clock = field(default_factory=lambda: Clock(freq=250e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        w, d, nw = int(self.bitwidth), int(self.depth), int(self.nword)
        spw, sh, b = int(self.samp_per_word), int(self.shift), int(self.base)
        bw = int(self.blk_words)
        if d & (d - 1):
            raise ValueError(f"buffer depth must be a power of two (got {d}): the wrap is a mask")
        if not 1 <= nw <= d:
            raise ValueError(
                f"a shot is {nw} words but the buffer holds {d}: a shot longer than the memory is "
                f"not a shot, it is a stream, and streaming is what waveflow.hw.rf_tx_stream is for")

        self.load = ShotLoopLoad(sim=self.sim, name=f"{self.name}_load", bitwidth=w, depth=d,
                                 nword=nw, samp_per_word=spw, base=b, clk=self.clk)
        self.play = type(self).player_cls(sim=self.sim, name=f"{self.name}_play", bitwidth=w,
                                          depth=d, nword=nw, base=b, blk_words=bw,
                                          dac_word_rate=self.dac_word_rate, clk=self.clk)
        # The re-layout is LAST, so it is the stage the converter back-pressures and therefore the
        # one that carries the block-shaped handover.  The accommodation follows the port.
        self.relayout = RfRelayoutToSlots(sim=self.sim, name=f"{self.name}_to_slots", bitwidth=w,
                                          n_slot=spw, shift=sh, blk_words=bw, clk=self.clk)
        # add_comp order is emit order and the DATA-FLOW order: command layer, playout, packing.
        for c in (self.load, self.play, self.relayout):
            self.add_comp(c)

        #: The one internal word channel.  Depth 2 — the HLS default for a top argument and enough
        #: for a producer and a consumer to overlap by one beat, which is all an II=1 chain needs.
        samp_if = StreamIF(name=f"{self.name}_samp_if", sim=self.sim, clk=self.clk, bitwidth=w,
                           depth=2)
        samp_if.bind(ep_name="master", endpoint=self.play.samp_out)
        samp_if.bind(ep_name="slave", endpoint=self.relayout.s_in)
        self.add_if(samp_if)

        # `mem`, not `buf`: the attribute name becomes the Verilog INSTANCE name and `buf` is a
        # primitive gate, which the wrapper emitter refuses by name rather than letting xvlog fail on
        # a syntax error that mentions no Python.
        self.mem = T2pBram(sim=self.sim, name=f"{self.name}_mem",
                           element_type=word_element(w), nelem=d)
        self.add_rtl_mod(self.mem)

        #: The lock, and the whole of the memory wiring.  One ``add_if`` files the two lock streams
        #: as internal edges and the two ``BramIF``\\ s as wrapper wires — see
        #: :meth:`~waveflow.hw.locked_mem.LockedT2pMemIF.rtl_interfaces`.
        self.lock = LockedT2pMemIF(name=f"{self.name}_lock_if", sim=self.sim, clk=self.clk,
                                   element_type=word_element(w), nelem=d, memory=self.mem)
        self.lock.bind("master", self.load.lock)
        self.lock.bind("slave", self.play.lock)
        self.add_if(self.lock)

        #: ``add_comp`` x ``add_endpoint`` order with every internally-bound endpoint removed.  The
        #: two ``buf_*`` entries are ports of the KERNEL, joined to the memory inside the wrapper.
        self.boundary = ["s_in", "resp_out", "buf_w", "buf_r", "samp_out"]

        # Convenience refs for testbenches — the boundary endpoints live on the children.
        self.s_in = self.load.s_in
        self.resp_out = self.load.resp_out
        self.samp_out = self.relayout.s_out

    # -- geometry, read off the graph rather than restated -------------------------------------

    @property
    def nsamp_shot(self) -> int:
        """Samples in one shot — what a host's ``ShotTxHdr.nsamp`` must equal."""
        return int(self.nword) * int(self.samp_per_word)

    @property
    def region(self) -> tuple[int, int]:
        """``[base, base + nword)``."""
        return self.load.region

    @property
    def is_identity(self) -> bool:
        """``True`` when the last stage's re-layout does nothing.  A gate should assert it is
        ``False``, or it is measuring a pair of wires rather than the conversion."""
        return int(self.shift) == 0

    @classmethod
    def for_word(cls, word, *, depth: int = BUF_DEPTH, nword: int = SHOT_WORDS, **kwargs):
        """Build the transmitter from the converter's **word type** — the single place the integers
        are derived.  A type cannot be an ``HwParam``, so what survives the call is integers."""
        from waveflow.hw.rf_relayout import check_geometry, slots_per_word
        from waveflow.hw.rfdc_samp_word import RfdcSampWord

        if not (isinstance(word, type) and issubclass(word, RfdcSampWord)):
            raise TypeError(
                f"RfShotTxLoop.for_word() takes the converter's WORD TYPE — the packing convention, "
                f"not a width. Got {word!r}.")
        check_geometry(word)
        return cls(bitwidth=int(word.bitwidth), samp_per_word=slots_per_word(word),
                   depth=int(depth), nword=int(nword), shift=int(word.justify_shift()), **kwargs)

    # -- counters ------------------------------------------------------------------------------

    @property
    def resps(self) -> list[tuple[int, int, int]]:
        """``(tid, status, nsamp_loaded)`` for every response, in the order they went out."""
        return list(self.load.resps)

    def assert_handover(self, n_loads: int, *, max_grant_seconds: float | None = None) -> None:
        """After a run: *n_loads* waveforms were handed over, cleanly, and the run ended idle.

        Four claims a byte comparison does not make, and each has its own failure:

        * the lock changed hands **exactly** *n_loads* times, on both sides;
        * every one of those was released, so the run did not end mid-handover;
        * the player actually **played filler** during them, which is what makes a handover visible
          on the output at all — a run with no filler in it never yielded anything;
        * the player came back, so the design is not sitting in the gap.
        """
        self.lock.assert_handover_happened(int(n_loads))
        if max_grant_seconds is not None:
            self.lock.assert_grant_bounded(float(max_grant_seconds))
        if int(self.load.n_stored) != int(n_loads):
            raise AssertionError(
                f"{type(self).__name__} '{self.name}': the loader stored {int(self.load.n_stored)} "
                f"shot(s), expected {int(n_loads)}. Responses: {self.resps}")
        if int(self.play.n_filler) < int(n_loads):
            raise AssertionError(
                f"{type(self).__name__} '{self.name}': the player emitted "
                f"{int(self.play.n_filler)} filler chunk(s) across {int(n_loads)} handover(s). A "
                f"handover that produced no gap is a handover the output cannot show, which means "
                f"the memory was yielded for no measurable time — or not at all.")
        if int(self.play.n_resumed) != int(n_loads):
            raise AssertionError(
                f"{type(self).__name__} '{self.name}': the player resumed "
                f"{int(self.play.n_resumed)} time(s) after {int(n_loads)} handover(s). A design "
                f"left in the gap plays filler forever and every counter above still looks right.")
        if not self.play.playing:
            raise AssertionError(
                f"{type(self).__name__} '{self.name}': the run ended with the player in filler. "
                f"Either the last release never arrived or it arrived after the horizon — and a "
                f"playout that stopped is invisible from the samples that did come out.")
