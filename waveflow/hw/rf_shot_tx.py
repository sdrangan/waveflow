r"""rf_shot_tx.py — **one** shot transmitter, on the lock, with both play modes.

``plans/rf_shot_unify.md``.  Stage A built this beside two predecessors — a finite player
(``ShotPhase`` + ``rdy`` + ``done``, five tasks) and an infinite one (``LockedT2pMemIF``, three
tasks) — and **Stage B deleted them both**, along with the ``ShotPhase`` buffer primitive underneath
the first, and moved their shared vocabulary in here.  What is left is one design that does what all
three did::

    s_in --[ShotTxHdr | samples ... TLAST]--> ShotTxLoader --[lock]--> [ BRAM ]
    resp_out <------------- ShotTxResp ----------  |  ^                    |
                                                 rep  done                |
                                                   v  |                   |
                                          ShotTxPlayer --[lock]-----------+
                                                   |
                                       RfRelayoutToSlots --> samp_out

**The two predecessors were complementary, not duplicates**, which is why Stage A was a build and
not a rename:

===============================  =======================  ==========================
                                 the finite one           the infinite one
===============================  =======================  ==========================
play                             finite ``nrepeat``       infinite
a load arriving mid-play         refused, ``SHOT_BUSY``   **preempts** via the lock
``SHOT_LOAD``                    yes                      *refused*
``SHOT_LOOP``                    —                        required
===============================  =======================  ==========================

Here both opcodes are legal, and **the play loop differs only in its exit condition**: finite counts
passes and stops, infinite does not.  Everything else — the chunk, the poll, the filler, the ordering
that the whole protocol turns on — is one body.

Why ``SHOT_BUSY`` survives, and only for the finite case
--------------------------------------------------------
Preempting a **finite** shot would silently truncate something the host explicitly asked for: it said
*play this n times*, and a design that stopped after two would produce a perfectly good, shorter
signal that nothing downstream could question.  Preempting an **infinite** one is the only way to
ever end it — a design that answered ``BUSY`` there would refuse every load forever.

So the loader refuses while a finite shot is in flight and does **not** request the lock; it accepts
and preempts otherwise.  That asymmetry is why the ``done`` token survives from the finite one: the
loader has to know when a finite shot has *finished*, and only the player knows.  The infinite path
neither sends nor needs one.

What the lock replaced
----------------------
The finite predecessor hand-wired seven internal channels and two
:class:`~waveflow.hw.bram.BramIF`\ s, and carried a ``ShotPhase`` object whose own docstring said it
was **pysim-only** — so its central safety claim never had an RTL witness.  Here one ``add_if(lock)``
files the two lock
streams as internal edges and both memory wires as wrapper wires, and the guard that replaces
``ShotPhase`` is :class:`~waveflow.hw.locked_mem._RegionGuard` in pysim *plus* the S2 measurement at
RTL.  Three channels remain that the lock does not own — ``rep``, ``done`` and ``samp`` — and each
carries something the lock has no opinion about.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar

import numpy as np

from waveflow.hw.bram import T2pBram, word_element
from waveflow.hw.clock import Clock
from waveflow.hw.codegen_targets import COMPOSITE_KERNEL
from waveflow.hw.dataschema import DataList, IntField
from waveflow.hw.hw_freerun import FreeRunMod
from waveflow.hw.hw_module import HwParam
from waveflow.hw.interface import (
    FramedStreamIFMaster,
    FramedStreamIFSlave,
    StreamIF,
    StreamIFMaster,
    StreamIFSlave,
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
from waveflow.simulation.simobj import ProcessGen

# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------

#: Default word width in bits — **64**, and that number is a measurement rather than a preference.
#: ``plans/adc_model.md`` § *Take 64 bits, not 56*: the integer serializer never straddles a word
#: boundary, so four 14-bit samples occupy the same word count at 64 bits as at a tight 56, byte
#: aligned, with 8 bits idle — and 64 is exactly the RFDC word width at four 16-bit slots, which
#: makes the logic-side re-layout a **pure re-layout inside one width**.
WORD_BW = 64

#: Default buffer depth in **words** (a power of two — the memory's address wrap is a mask).
BUF_DEPTH = 1024

#: Default words in one shot.  Not the depth: a shot shorter than the memory is the ordinary case,
#: and the two being separate is what lets a gate exercise a partial buffer.
SHOT_WORDS = 256

# ---------------------------------------------------------------------------
# The opcode
# ---------------------------------------------------------------------------

#: Load the samples that follow this header, then play them.
SHOT_LOAD = 0
#: A **fence**: take no payload, change nothing, and answer.
#:
#: The name is ``examples/stream_inband``'s, and the reason it is here is the schema's: a stream of
#: commands wants a terminator.  What it *does* is different, and the difference is the execution
#: model rather than a choice.  ``PolyAccel`` is :class:`~waveflow.hw.hw_hostactivated.HostActivated`
#: — a ``while (True)`` inside ``on_start`` that ``END`` breaks so the kernel can return and raise
#: ``ap_done``.  :class:`ShotTxLoader` is an ``hls::task``: **there is no loop to break**, because the
#: task runtime re-fires the body forever and an ``ap_ctrl_none`` design has no ``return`` to reach.
#:
#: So ``END`` is worth having for what its *response* proves rather than for what it stops.  The
#: loader answers commands strictly in order, so a ``ShotTxResp`` for an ``END`` says every command
#: ahead of it has been processed — which is what a testbench needs to know it may stop looking, and
#: what a host needs before tearing a transfer down.  It is a quiescence probe, and a testbench that
#: ended by timing out instead could not tell a finished run from a deadlocked one.
SHOT_END = 1
#: Load the samples that follow, then play them **until told otherwise** — the infinite-play flag.
#:
#: ``plans/t2p_lock_chan.md`` S1.  :data:`SHOT_LOAD` and this one differ in exactly one thing: when
#: the design stops.  A ``SHOT_LOAD`` plays ``nrepeat`` times and goes quiet, which is why a second
#: load arriving mid-play is :data:`SHOT_BUSY` — the memory is under a reader.  A ``SHOT_LOOP`` never
#: goes quiet, so ``SHOT_BUSY`` would refuse every load forever; the design that accepts it hands the
#: memory over instead, and ``nrepeat`` is not read.
#:
#: **It is an opcode rather than a bit on ``nrepeat``**, and not for tidiness: ``nrepeat == 0``
#: already means something — it is what the loader sends for a shot it refused to call playable — so
#: overloading it would make "play forever" and "never play" the same value on the wire.
#:
#: :class:`~waveflow.hw.rf_shot_tx.RfShotTx` does not implement it (a finite player has nothing to
#: Until ``plans/rf_shot_unify.md`` Stage B there were two designs and each implemented exactly one
#: of the pair; :class:`RfShotTx` implements both, and this constant is why the two could share a
#: header schema across that whole period rather than growing a second vocabulary for one opcode.
SHOT_LOOP = 2

# ---------------------------------------------------------------------------
# The verdict
# ---------------------------------------------------------------------------

#: The shot is in the memory and is playable.
SHOT_LOADED = 0
#: ``TLAST`` arrived before the shot was full — **the status this response exists for**.  A short
#: transfer completes cleanly at the DMA, so the host sees success while the buffer holds a block of
#: the right shape carrying half a signal.  Nothing on the host side can see it.
SHOT_SHORT = 1
#: ``nsamp`` disagrees with the shot the buffer was built for.  Build-time structure
#: (:attr:`RfShotTx.nword`) is the single source for the length; a command
#: that disagreed is refused rather than truncated, because a truncated waveform is data of the wrong
#: duration and plays as a quieter, shorter signal.
SHOT_WRONG_LEN = 2
#: The header arrived while a shot was playing.  Refused **at the command**, before a word is taken,
#: rather than asserted after the fact — which is what the retired ``ShotPhase`` did, in pysim only.
SHOT_BUSY = 3
#: ``nsamp == 0``.  Refused for the reason :data:`~waveflow.hw.rf_tx_stream.TX_ZERO_LEN` gives next
#: door: a zero-length load has nothing to complete on, so it can never resolve.
SHOT_ZERO_LEN = 4

#: Human-readable names, so an assertion says what happened rather than a number.
SHOT_STATUS_NAMES = {
    SHOT_LOADED: "SHOT_LOADED",
    SHOT_SHORT: "SHOT_SHORT",
    SHOT_WRONG_LEN: "SHOT_WRONG_LEN",
    SHOT_BUSY: "SHOT_BUSY",
    SHOT_ZERO_LEN: "SHOT_ZERO_LEN",
}

IdxField = IntField.specialize(bitwidth=IDX_BW, signed=False)
OpField = IntField.specialize(bitwidth=8, signed=False)


# ---------------------------------------------------------------------------
# The in-band header, and the verdict that answers it
# ---------------------------------------------------------------------------

class ShotTxHdr(DataList):
    r"""The header that rides ahead of the samples on the same stream.

    Named so it cannot be confused with the two ``TxCmd``\ s that already exist, which
    ``plans/rf_shot_buf.md`` § *The commands* asks for explicitly:
    :class:`waveflow.hw.rf_tx_stream.TxCmd` names a **schedule**,
    :class:`waveflow.hw.rf_samp_buf_tx.TxCmd` names a **buffer window**, and this one names a
    **stream transaction**.  The three designs are alternatives, never layers.

    **There is no length-of-shot field.**  How many words a shot is, is build-time structure declared
    once on :attr:`RfShotTx.nword`; a command that restated it would be a second source that could
    disagree — the discipline :class:`~waveflow.hw.rfdc.Rfdc` follows by reading ``samp_rate`` off the
    clock rather than declaring its own.  ``nsamp`` is here because it is what the *host* believes it
    is sending, and catching that belief disagreeing with what arrived is the verdict's whole job.
    """

    include_filename: ClassVar[str | None] = "rf_shot_tx_hdr.h"
    elements = {
        "opcode":  {"schema": OpField, "description": "SHOT_LOAD or SHOT_END"},
        "tid":     {"schema": IdxField, "description": "transaction id, echoed on the response"},
        "nsamp":   {"schema": IdxField, "description": "samples the host is sending (0 for END)"},
        "nrepeat": {"schema": IdxField,
                    "description": "times to play the shot once loaded (>= 1)"},
    }


class ShotTxResp(DataList):
    """One response per header, and exactly one — see ``plans/rf_shot_buf.md`` § *Why no
    ``has_response`` flag*: there is no configuration in which a command is issued and nobody wants to
    know whether it worked.

    ``nsamp_loaded`` is **what actually landed**, not what was asked for.  On :data:`SHOT_LOADED` the
    two agree; on :data:`SHOT_SHORT` the difference *is* the diagnosis, and it is the number a DMA
    cannot produce — ``sendchannel.transfer()`` knows it pushed bytes, not whether they were a whole
    waveform.
    """

    include_filename: ClassVar[str | None] = "rf_shot_tx_resp.h"
    elements = {
        "tid":          {"schema": IdxField, "description": "the header's transaction id"},
        "status":       {"schema": IdxField,
                         "description": "SHOT_LOADED / SHORT / WRONG_LEN / BUSY / ZERO_LEN"},
        "nsamp_loaded": {"schema": IdxField,
                         "description": "samples actually written to the buffer"},
    }


#: Schema classes a build emits C++ headers for.  Two, and only two — the command layer's whole
#: vocabulary is a header in and a verdict out.  Compare
#: :data:`~waveflow.hw.rf_tx_stream.TX_STREAM_SCHEMA_CLASSES`, which is four because the streaming
#: transmitter also has to say things to *itself* (a tagged sample, a per-window status); the shot
#: design has nothing to arbitrate, so it has nothing internal to name.
SHOT_TX_SCHEMA_CLASSES = [ShotTxHdr, ShotTxResp]



#: What the player emits while it is not playing — between shots, during a handover, and after a
#: finite play-set has finished.  **Zero, and it is a value rather than a stall**: the player cannot
#: stop, so a body that blocked here would back-pressure the converter.
FILLER = 0

class ShotPlayCmd(DataList):
    """Loader -> player: *what to do with the shot you are about to be handed back*.

    **It carries the host's own opcode**, not a parallel vocabulary invented for the internal wire.
    The player needs exactly two things the lock has no opinion about — how many passes, and whether
    anyone is waiting for a ``done`` — and both are already in the header the host sent.  A second
    encoding would be a second thing to keep in step.

    Two rules, and between them they cover every case both predecessors had:

    * **``nrepeat == 0`` means play nothing.**  That is ``RfShotTx``'s existing convention for a shot
      that was loaded and must not reach the converter (a :data:`~waveflow.hw.rf_shot_tx.SHOT_SHORT`
      one), and it needs no third mode to express.
    * **``opcode`` says who is waiting.**  :data:`~waveflow.hw.rf_shot_tx.SHOT_LOAD` means the loader
      is blocked on a ``done`` and the player owes it one; :data:`~waveflow.hw.rf_shot_tx.SHOT_LOOP`
      means nobody is waiting and the player must not send one.  A spurious ``done`` would clear a
      ``busy`` that a *later* finite shot set, and the next load would preempt it — the exact
      truncation ``SHOT_BUSY`` exists to prevent.

    It is a schema rather than a raw word because a sentinel in a bare ``ap_uint`` is how a design
    ends up comparing against a magic number in two places.
    """

    include_filename: ClassVar[str | None] = "shot_play_cmd.h"
    elements = {
        "opcode":  {"schema": OpField,
                    "description": "SHOT_LOAD (a done is owed) or SHOT_LOOP (none is)"},
        "nrepeat": {"schema": IdxField,
                    "description": "passes to play; 0 means play nothing"},
    }


#: Schema classes a build emits C++ headers for.  ``ShotTxHdr`` and ``ShotTxResp`` are **not** here:
#: they are still :mod:`waveflow.hw.rf_shot_tx`'s at Stage A — see the ownership decision recorded in
#: ``plans/rf_shot_unify.md``.  A build wanting this design needs both lists.
SHOT_PLAY_SCHEMA_CLASSES = [ShotPlayCmd]


# ---------------------------------------------------------------------------
# The loader — the REQUESTER
# ---------------------------------------------------------------------------

@dataclass
class ShotTxLoader(FreeRunMod):
    r"""Read a frame, decide, take the region, write it, give it back, tell the player, answer.

    The requester side of the lock.  It does ``RfShotTx``'s command work and
    the infinite predecessor's memory write in one body: there is no separate buffer task, because
    the lock
    is what makes writing the memory directly safe.

    **All five verdicts are reachable here**, which is the merge:

    ==============================================  ==================================
    ``nsamp == 0``                                  :data:`~waveflow.hw.rf_shot_tx.SHOT_ZERO_LEN`
    ``nsamp`` disagrees with ``nword * spw``        :data:`~waveflow.hw.rf_shot_tx.SHOT_WRONG_LEN`
    an opcode that is neither ``LOAD`` nor ``LOOP`` :data:`~waveflow.hw.rf_shot_tx.SHOT_WRONG_LEN`
    a **finite** shot is still playing              :data:`~waveflow.hw.rf_shot_tx.SHOT_BUSY`
    ``TLAST`` before the shot was full              :data:`~waveflow.hw.rf_shot_tx.SHOT_SHORT`
    otherwise                                       :data:`~waveflow.hw.rf_shot_tx.SHOT_LOADED`
    ==============================================  ==================================

    **Malformed before transient**, which is the repo's order and for its reason: a command that is
    wrong *and* badly timed should be told the thing it can fix.  Retry repairs a ``BUSY``; nothing
    repairs a length the buffer was not built for.

    **``busy`` covers both opcodes.**  A ``SHOT_LOOP`` arriving while a *finite* shot plays is
    refused too — the objection is not to what the new shot is, it is that truncating the running
    one would be invisible.  A load of either kind arriving while an *infinite* shot plays is
    accepted and preempts it.
    """

    cpp_kernel_name: ClassVar[str | None] = "shot_tx_loader"

    #: Word width in bits — the host port's and the memory's.
    bitwidth: HwParam[int] = WORD_BW
    #: Memory depth in **elements**; the bound the region is checked against.
    depth: HwParam[int] = BUF_DEPTH
    #: Words in one shot.  Build-time structure and the single source for the length: the header's
    #: ``nsamp`` is what the *host* believes, and catching the two disagreeing is what
    #: :data:`~waveflow.hw.rf_shot_tx.SHOT_WRONG_LEN` is.
    nword: HwParam[int] = SHOT_WORDS
    #: Samples one word carries — what turns a word count into the ``nsamp`` a host speaks in.
    samp_per_word: HwParam[int] = 4
    #: First element of the region.  **Non-zero is the interesting case**: ``base + offset`` is the
    #: shape of the byte-versus-word bug ``bram_toy`` stayed green through.
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
        #: The host's port: header **and** payload, one frame, ``TLAST`` at the end.  Without the pin
        #: there is no in-band way to say *that was the end* — a payload word and a header word are
        #: the same bits — so a short transfer would stall a counted loop, and a hang is
        #: indistinguishable from a deadlock.
        self.s_in = FramedStreamIFSlave(sim=self.sim, name=f"{self.name}_s_in", bitwidth=w,
                                        has_tlast=True)
        #: The verdict, with its own ``TLAST`` so a host reading through a DMA S2MM channel learns
        #: where one response ends without being told how long it is.
        self.resp_out = FramedStreamIFMaster(sim=self.sim, name=f"{self.name}_resp", bitwidth=w,
                                             has_tlast=True)
        #: One endpoint, three channels: the memory port, the lock command out, the lock response in.
        self.lock = LockedMemMasterIF(sim=self.sim, name=f"{self.name}_lock",
                                      element_type=word_element(w), nelem=d, access="write")
        #: What to do with the shot just written — see :class:`ShotPlayCmd`.
        self.rep_out = StreamIFMaster(sim=self.sim, name=f"{self.name}_rep", bitwidth=w,
                                      has_tlast=True)
        #: One token per completed **finite** play-set.  The only thing that clears
        #: :attr:`busy`, and the only reason this module has an input other than the host's.
        self.done_in = StreamIFSlave(sim=self.sim, name=f"{self.name}_done", bitwidth=w,
                                     has_tlast=True)
        # ORDER MATTERS: `boundary` is derived from add_comp x add_endpoint order, so s_in, resp_out
        # and the lock's memory port must be registered before the two internal tokens.
        for ep in (self.s_in, self.resp_out, self.lock, self.rep_out, self.done_in):
            self.add_endpoint(ep)
        #: ``(tid, status, nsamp_loaded)`` for every response, in the order they went out.
        self.resps: list[tuple[int, int, int]] = []
        #: Shots actually written to the memory.  Counted rather than inferred from the verdicts: a
        #: run whose loader answered correctly and stored nothing passes every response check.
        self.n_stored = 0
        #: A **finite** shot is in flight.  Sim-only bookkeeping — the C++ twin holds the same one
        #: bit in a ``static`` — and it is what :data:`~waveflow.hw.rf_shot_tx.SHOT_BUSY` reads.
        self.busy = 0

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
        return KernelTask("shot_tx_loader_task", "shot_tx_loader_task.h",
                          ("s_in", "done_in", "lock", "rep_out", "resp_out"),
                          template_args=(int(self.bitwidth), int(self.depth), int(self.nword),
                                         int(self.samp_per_word), int(self.base)))

    # -- the pysim twin ----------------------------------------------------------------------

    def _verdict(self, hdr) -> int:
        """The header-only refusals, in the order the C++ body tests them."""
        op = int(hdr.opcode)
        if op not in (SHOT_LOAD, SHOT_LOOP):
            return SHOT_WRONG_LEN
        if int(hdr.nsamp) == 0:
            return SHOT_ZERO_LEN
        if int(hdr.nsamp) != self.nsamp_shot:
            return SHOT_WRONG_LEN
        if self.busy:
            # A FINITE shot is running.  Refused rather than preempted: the host asked for a number
            # of passes, and stopping early would produce a perfectly good shorter signal that
            # nothing downstream could question.
            return SHOT_BUSY
        return SHOT_LOADED

    def run_iter(self) -> ProcessGen[None]:
        """One firing is one frame, which is one iteration of the C++ body.

        ``get()`` with no count returns **the whole burst**, and a burst is a ``TLAST``-delimited
        frame — so the read of "the header, then words until last" is one call and the two backends
        see the same boundary rather than two encodings of it.

        **A known twin divergence, carried over from the infinite predecessor and re-stated because
        it is
        still true.**  The C++ acquires the region and *then* reads the payload beat by beat, because
        ``buf[lo + i] = s_in.read()`` at II=1 is one pipeline.  This body cannot: the frame is in hand
        before there is anything to decide.  So the RTL holds the region for the whole **transfer**
        and pysim holds it for the **write**, and the handover gap differs between the backends.
        Both are measured; neither is inherited.
        """
        w, nw = int(self.bitwidth), int(self.nword)
        frame = np.asarray((yield from self.s_in.get()), dtype=np.uint64).ravel()

        hn = ShotTxHdr.nwords_per_inst(w)
        # The schema's own deserializer, never a field walk: the layout has one author and the
        # generated C++ reads it through the same statement.
        hdr = ShotTxHdr().deserialize(frame[:hn], word_bw=w)
        payload = frame[hn:]

        # Harvested AFTER the header has arrived, so `busy` is as fresh as it can be when it is read.
        # NON-BLOCKING: a finite shot still running is the ANSWER (SHOT_BUSY), not a reason to wait.
        if (yield from self.done_in.get_nb(nwords_max=1)) is not None:
            self.busy = 0

        if int(hdr.opcode) == SHOT_END:
            # A FENCE, not a halt.  An hls::task has no loop to break, so what END is worth is what
            # its RESPONSE proves: headers are answered strictly in order, so this one says
            # everything ahead of it has been processed.
            yield from self._answer(hdr, SHOT_LOADED, 0)
            return

        status = self._verdict(hdr)
        if status != SHOT_LOADED:
            # Nothing to drain: the frame arrived whole, so a refused command leaves no residue on
            # the wire to become the next header.  That is the framed port's doing, not this body's.
            yield from self._answer(hdr, status, 0)
            return

        took = min(int(payload.size), nw)
        # A short frame is completed with ZEROS so the region is fully written -- and then not
        # played, on EITHER path.  The infinite predecessor played the padded result, because it had
        # no way to
        # go quiet; this design does, so half a waveform never reaches the converter.
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

        if took < nw:
            status = SHOT_SHORT                 # THE verdict this response exists for

        # THE PLAY COMMAND GOES OUT BEFORE THE RELEASE, and the player reads it on the release.
        # Ordering the two writes this way is what makes the player's read of it a bounded wait
        # rather than a guess -- and the player blocks for it, so even a scheduler that reordered
        # them would be correct, only slower by a beat.
        cmd = ShotPlayCmd()
        cmd.opcode = int(hdr.opcode)
        cmd.nrepeat = int(hdr.nrepeat) if status == SHOT_LOADED else 0
        yield from self.rep_out.write(cmd)
        # A BARRIER, not a hint: the player may resume the instant it sees this.
        yield from self.lock.release()
        self.n_stored += 1
        if int(hdr.opcode) == SHOT_LOAD:
            # Only a finite shot makes the design busy, and only a finite shot is owed a `done`.
            self.busy = 1

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
class ShotTxPlayer(FreeRunMod):
    r"""Play the region — a counted number of passes, or forever — and yield it on request.

    The owner side of the lock: it holds the whole memory by default, it cannot stop, and it polls
    the command channel exactly once per :attr:`blk_words` elements of its own work.

    **The play loop differs from the infinite predecessor's in exactly one place**, and it is the
    wrap::

        if self.rd == 0 and not self.loop:      # a pass has just finished
            self.nrep_left -= 1
            if self.nrep_left == 0:
                self.playing = False            # ... and go quiet
                yield from self.done_out.write(...)

    Everything else — the chunk, the anchoring, the filler, the poll, the ordering — is shared.  That
    is the whole content of the merge: two designs that differed only in an exit condition.

    **The one ordering everything turns on**::

        self.playing = False            # STOP TOUCHING IT ...
        yield from self.lock.grant(...) # ... THEN grant

    Granting while still reading lets the loader write memory this task is reading.
    :meth:`~waveflow.hw.locked_mem.LockedMemSlaveIF.grant` takes the region out of the owner's hands
    before the answer goes on the wire, so getting the two lines the wrong way round **raises on the
    very next chunk** instead of returning a plausible sample.

    **The ``done`` token is owed only on the finite path**, and a spurious one is worse than none: it
    would clear a ``busy`` that a *later* finite shot set, and the next load would preempt it — the
    truncation ``SHOT_BUSY`` exists to prevent.  :class:`ShotPlayCmd` carries the host's opcode for
    exactly this reason.

    **Statics, and therefore the reset trap.**  ``rd``, ``playing``, ``loop`` and ``nrep_left`` are
    carried across firings, and this body **writes before it reads** — writing without being asked is
    what *the side that cannot stop* means.  The C++ twin carries ``#pragma HLS reset`` on each and
    the build needs ``config_rtl -reset state``.
    """

    cpp_kernel_name: ClassVar[str | None] = "shot_tx_player"

    bitwidth: HwParam[int] = WORD_BW
    depth: HwParam[int] = BUF_DEPTH
    #: Words in one shot — the region's length, and the wrap point of the read pointer.
    nword: HwParam[int] = SHOT_WORDS
    #: First element of the region.  Must match the loader's; the composite passes one number.
    base: HwParam[int] = 0
    #: Elements between polls, **and** words per pysim output burst — one number, because they are
    #: the same boundary.  A poll per converter block is the natural cadence.
    blk_words: HwParam[int] = 1
    #: **Words per second the DAC consumes** — or ``None`` to run at the fabric's rate alone.  A
    #: modelling input and not hardware: in RTL this task is paced by ``TREADY``, but pysim does not
    #: back-pressure a burst write, so the metronome has to be handed over.
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
                f"blk_words={bw} does not divide a {nw}-word shot. A chunk that straddled the end "
                f"of the region would need two base additions, and the play boundary would stop "
                f"landing on a block boundary.")
        self.lock = LockedMemSlaveIF(sim=self.sim, name=f"{self.name}_lock",
                                     element_type=word_element(w), nelem=d, access="read",
                                     check_period=bw)
        self.samp_out = StreamIFMaster(sim=self.sim, name=f"{self.name}_samp", bitwidth=w,
                                       has_tlast=True)
        self.rep_in = StreamIFSlave(sim=self.sim, name=f"{self.name}_rep", bitwidth=w,
                                    has_tlast=True)
        self.done_out = StreamIFMaster(sim=self.sim, name=f"{self.name}_done", bitwidth=w,
                                       has_tlast=True)
        # ORDER MATTERS: the lock's memory port must be registered before the internal tokens, so
        # `boundary` picks it up in the position the composite names.
        for ep in (self.lock, self.samp_out, self.rep_in, self.done_out):
            self.add_endpoint(ep)

        #: The read pointer **within the region** — a ``static`` in the C++ twin.
        self.rd = 0
        #: ``True`` while it owns the region *and* has something to play.  **Starts false**: nothing
        #: has been loaded yet, and playing a memory that was never written is a plausible sample
        #: rather than a silence.
        self.playing = False
        #: ``True`` when the current shot is a ``SHOT_LOOP``.  The exit condition, and the only
        #: difference between the two predecessors' players.
        self.loop = False
        #: Passes left on a finite shot.  Meaningless while :attr:`loop`.
        self.nrep_left = 0
        #: Chunks emitted, chunks that were filler, and words that came out of the memory.  All
        #: three, because a run that played nothing but filler looks identical to a run that played
        #: nothing at all, and a truncated play carries the right samples as far as it got.
        self.n_chunks = 0
        self.n_filler = 0
        self.n_words = 0
        #: Passes completed, handovers resumed, and ``done`` tokens sent.
        self.n_plays = 0
        self.n_resumed = 0
        self.n_done = 0
        #: The pysim rate grid: when the first block went out, and how many have.
        self._t0: float | None = None
        self._blocks = 0

    def kernel_task(self) -> KernelTask:
        return KernelTask("shot_tx_player_task", "shot_tx_player_task.h",
                          ("lock", "rep_in", "done_out", "samp_out"),
                          template_args=(int(self.bitwidth), int(self.depth), int(self.nword),
                                         int(self.base), int(self.blk_words)))

    def run_iter(self) -> ProcessGen[None]:
        """One firing is one chunk **and exactly one poll** — the C++ body's outer iteration."""
        yield from self._chunk_and_pace()

        # EXACTLY ONE POLL, outside everything above -- that is what `check_period` means and what
        # keeps the datapath's II untouched.  `handle_nb` applies a RELEASE on the spot because a
        # release needs no decision and no answer; an ACQUIRE comes back untouched because granting
        # it is this body's call and must follow the state change.
        cmd = yield from self.lock.handle_nb()
        if cmd is None:
            return
        if int(cmd.opcode) == LOCK_ACQUIRE:
            self.playing = False                    # STOP TOUCHING IT ...
            yield from self.lock.grant(int(cmd.start_addr), int(cmd.end_addr))   # ... THEN grant
            return

        # A RELEASE.  The play command is already on its channel -- the loader wrote it first -- so
        # this read is a bounded wait rather than a guess.  BLOCKING, and safe: it is
        # control-dependent on the poll's result, so nothing can hoist it, and it costs at most a
        # beat inside a gap the design is already in.
        play = yield from self.rep_in.get_schema(ShotPlayCmd)
        # A new waveform starts at its beginning: resuming mid-shot would splice the tail of the old
        # waveform's phase onto the new one, which is right in no application and is invisible from a
        # word count.
        self.rd = 0
        self.loop = int(play.opcode) == SHOT_LOOP
        self.nrep_left = int(play.nrepeat)
        self.playing = self.nrep_left > 0
        self.n_resumed += 1
        if not self.playing and not self.loop:
            # A finite shot that must not play -- a SHORT one.  The loader is blocked on a `done`
            # and nothing else will ever send it.
            yield from self._send_done()

    def _chunk_and_pace(self) -> ProcessGen[None]:
        """One chunk out — from the memory when it owns it, filler when it does not — then the rate.

        Split from :meth:`run_iter` so the datapath and the *decision* are separable: the poll is one
        firing's worth of lock traffic sitting outside a body that is otherwise a counted loop, which
        is exactly the shape the C++ has and exactly why ``II=1`` survives having a lock at all.

        The rate is charged **after** the hand-off and on an **absolute** grid.  Charging first would
        make every block arrive one period late; a relative wait restarts from wherever ``now``
        happens to be, so everything the body yielded for is added to the period and never given back
        — the defect that made ``rf_samp_buf_tx``'s player slip a whole block every fourth firing.
        """
        w, bw, nw = int(self.bitwidth), int(self.blk_words), int(self.nword)
        if self.playing:
            data, t0 = yield from self.lock.read_pipelined(word_element(w), bw,
                                                           addr=int(self.base) + self.rd)
            yield from self.samp_out.write_pipelined(data, t_out_start=t0)
            self.n_words += bw
            self.rd += bw
            if self.rd >= nw:
                # A pass has just finished.  THE ONE PLACE THE TWO PREDECESSORS DIFFER.
                self.rd = 0
                self.n_plays += 1
                if not self.loop:
                    self.nrep_left -= 1
                    if self.nrep_left <= 0:
                        self.playing = False
                        yield from self._send_done()
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

    def _send_done(self) -> ProcessGen[None]:
        """Tell the loader a finite play-set is over.  **Never on the loop path.**

        A spurious token would clear a ``busy`` that a later finite shot set, and the next load would
        preempt it — which is the truncation ``SHOT_BUSY`` exists to prevent, arrived at from the
        other side.
        """
        self.n_done += 1
        yield from self.done_out.write(np.array([1], dtype=np.uint64))


# ---------------------------------------------------------------------------
# The composite
# ---------------------------------------------------------------------------

@dataclass
class RfShotTx(FreeRunMod):
    r"""The whole transmitter as one design scope — finite play and infinite play, one design.

    Three ``hls::task``\ s and one memory beside them::

        s_in --> ShotTxLoader --[lock]--> [ BRAM ] --[lock]--> ShotTxPlayer --> RfRelayoutToSlots
                   |   ^ done                                       |                    |
               resp_out +--------------- rep ---------------------->+                samp_out

    **It was built under the name ``RfShotTxUnified``**, beside the two designs it merges, because
    Stage A of ``plans/rf_shot_unify.md`` was forbidden to touch either: if the merge had turned out
    harder than it looked, the working designs had to still be there.  Stage B deleted them and this
    class took the freed name.

    What the lock removed, counted
    ------------------------------
    The finite predecessor wired **seven** internal channels and **two** ``BramIF``\ s by hand
    (``pay rep rdy_load rdy_play dense samp done`` + ``bufw bufr``), instantiated **five** tasks, and
    shared a pysim-only ``ShotPhase`` object between two of them.  This wires **three**
    (``rep done samp``) and one ``add_if(lock)``, and instantiates three.  The four that vanished —
    ``pay``, ``rdy_load``, ``rdy_play``, ``dense`` — existed only to move samples between tasks the
    lock made unnecessary, and ``ShotPhase`` is replaced by a guard that also exists at RTL.

    ``rep`` and ``done`` survive because the lock has no opinion about them: one says *what to play*
    and the other says *a finite shot has finished*, and neither is a question about who may touch
    which addresses.
    """

    cpp_kernel_name: ClassVar[str | None] = "rf_shot_tx"
    potential_targets: ClassVar[frozenset[str]] = frozenset({COMPOSITE_KERNEL})

    #: The player class this composite instantiates.  A ClassVar, so it is changed by subclassing and
    #: never by a caller — the seam an example uses to build a deliberately broken twin without
    #: copying the composite.  The infinite predecessor carried the same seam, for the same reason.
    player_cls: ClassVar[type] = ShotTxPlayer

    #: Word width in bits — one number for the host port, the memory, the player and the converter.
    bitwidth: HwParam[int] = WORD_BW
    #: Samples one word carries.
    samp_per_word: HwParam[int] = 4
    #: Memory depth in **WORDS** (a power of two: the address wrap is a mask).
    depth: HwParam[int] = BUF_DEPTH
    #: Words in one shot.  ``base + nword <= depth``.
    nword: HwParam[int] = SHOT_WORDS
    #: First element of the region.  **One number, passed to both ends**, because a loader and a
    #: player that disagreed about where the waveform is would each be individually correct.
    base: HwParam[int] = 0
    #: Bits the effective sample sits above the bottom of its converter slot.  **0 makes the last
    #: stage the identity**, so a build that leaves it there is measuring a pair of wires.
    shift: HwParam[int] = 2
    #: Words per converter block: the player's chunk, its poll period, and the re-layout's pysim
    #: burst.  One number for all three because they are one boundary.
    blk_words: HwParam[int] = 1
    #: The DAC's word rate — :attr:`ShotTxPlayer.dac_word_rate`, passed through.  Not hardware.
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

        self.load = ShotTxLoader(sim=self.sim, name=f"{self.name}_load", bitwidth=w, depth=d,
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

        # -- the three channels the lock does NOT own ------------------------------------------
        #
        # `rep` and `done` are depth 1 because there is exactly one of each in flight by
        # construction: a play command is written once per accepted load, and a `done` cannot
        # accumulate because a second finite load is refused until the first is harvested.  `samp` is
        # depth 2 -- the HLS default for a top argument and enough for a producer and a consumer to
        # overlap by one beat, which is all an II=1 chain needs.
        for nm, master, slave, depth in (("rep", self.load.rep_out, self.play.rep_in, 1),
                                         ("done", self.play.done_out, self.load.done_in, 1),
                                         ("samp", self.play.samp_out, self.relayout.s_in, 2)):
            ifc = StreamIF(name=f"{self.name}_{nm}_if", sim=self.sim, clk=self.clk, bitwidth=w,
                           depth=depth)
            ifc.bind(ep_name="master", endpoint=master)
            ifc.bind(ep_name="slave", endpoint=slave)
            self.add_if(ifc)

        # `mem`, not `buf`: the attribute name becomes the Verilog INSTANCE name and `buf` is a
        # primitive gate, which the wrapper emitter refuses by name rather than letting xvlog fail on
        # a syntax error that mentions no Python.
        self.mem = T2pBram(sim=self.sim, name=f"{self.name}_mem",
                           element_type=word_element(w), nelem=d)
        self.add_rtl_mod(self.mem)

        #: The lock, and the whole of the memory wiring.  One ``add_if`` files the two lock streams
        #: as internal edges and the two ``BramIF``\\ s as wrapper wires.
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
                f"RfShotTx.for_word() takes the converter's WORD TYPE — the packing "
                f"convention, not a width. Got {word!r}.")
        check_geometry(word)
        return cls(bitwidth=int(word.bitwidth), samp_per_word=slots_per_word(word),
                   depth=int(depth), nword=int(nword), shift=int(word.justify_shift()), **kwargs)

    # -- counters and verdicts -------------------------------------------------------------------

    @property
    def resps(self) -> list[tuple[int, int, int]]:
        """``(tid, status, nsamp_loaded)`` for every response, in the order they went out."""
        return list(self.load.resps)

    @property
    def n_plays(self) -> int:
        """Passes the player finished."""
        return int(self.play.n_plays)

    def assert_handover(self, n_loads: int, *, max_grant_seconds: float | None = None) -> None:
        """After a run: *n_loads* waveforms were handed over, and every one of them cleanly.

        Claims a byte comparison does not make.  The lock changed hands exactly *n_loads* times on
        both sides; every grant was released, so the run did not end mid-handover; the player played
        filler, which is what makes a handover visible on the output at all; and it resumed once per
        handover, so the design is not sitting in a gap.
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
                f"handover that produced no gap is a handover the output cannot show.")
        if int(self.play.n_resumed) != int(n_loads):
            raise AssertionError(
                f"{type(self).__name__} '{self.name}': the player resumed "
                f"{int(self.play.n_resumed)} time(s) after {int(n_loads)} handover(s). A design "
                f"left in the gap plays filler forever and every counter above still looks right.")

    def assert_finite_completed(self, n_shots: int, n_plays: int | None = None) -> None:
        """After a **finite** run: every finite shot ended, and every one was accounted for.

        Two separate failures.  A player that never stopped produces a longer perfectly good signal;
        one that never sent its ``done`` leaves the loader permanently busy — every later load
        answers ``SHOT_BUSY`` and the design looks like it is working right up until a host tries to
        change waveform.

        *n_plays* is **total** passes, finite and infinite alike, so it is optional: a run that mixes
        the two has a loop-pass count that depends on when the preemption landed, and pinning it
        would be pinning the scheduler rather than the design.  Pass it only where every pass was
        finite.
        """
        if n_plays is not None and int(self.play.n_plays) != int(n_plays):
            raise AssertionError(
                f"{type(self).__name__} '{self.name}': the player finished "
                f"{int(self.play.n_plays)} pass(es), expected {int(n_plays)}. A play that stopped "
                f"early carries the right samples as far as it got, so only the count says so.")
        if int(self.play.n_done) != int(n_shots):
            raise AssertionError(
                f"{type(self).__name__} '{self.name}': the player sent {int(self.play.n_done)} "
                f"done token(s) for {int(n_shots)} finite shot(s). A missing one leaves the loader "
                f"permanently busy, and every later load answers SHOT_BUSY.")
        if self.play.playing:
            raise AssertionError(
                f"{type(self).__name__} '{self.name}': the run ended with the player still playing "
                f"a finite shot. It was asked for a fixed number of passes and did not stop.")
