"""rf_shot_tx.py — ``plans/rf_shot_buf.md`` Stage B: **TX — play a stored waveform**.

Stage A built the primitive (:mod:`waveflow.hw.rf_shot_buf`): a memory, a loader, a reader, and a
``rdy`` token between them.  It had no command, no payload source and no verdict, because it had
nothing to answer.  This module adds those three and nothing else::

    DMA (MM2S) --AXIS: [ShotTxHdr][samples ... TLAST]--> RfShotBufLoad --> BRAM
    DMA (S2MM) <---------------- ShotTxResp -----------                     | rdy
                                                                            v
                                     RfShotBufRead --> RfRelayoutToSlots --> Rfdc.tx_streams[0]

The payload is **in-band on the stream**, the shape ``examples/stream_inband`` teaches: a header
ahead of the samples, ``TLAST`` marking the end, and a persistent loop that halts on ``END``.

Why in-band, and not the ``m_axi`` arena the plan first chose
-------------------------------------------------------------

``plans/rf_shot_buf.md`` § *Where the payload comes from* chose ``m_axi`` with an in-band address on
2026-08-24, and § *Decisions a session must not re-open* listed it.  **Re-opened and reversed
2026-08-31**, for a constraint that was not on the table when it was written: this design is handed
to someone wiring it in Vivado IPI by hand and driving it from PYNQ.  The plan's own case for
``m_axi`` is RX-driven and concedes *"TX is the weaker case"*; TX-before-RX is a separate settled
decision, so Stage C stays free to choose its own transport with capture evidence in hand.

Three things decided it, and none is development time:

* **The port was already a stream.**  :class:`~waveflow.hw.rf_shot_buf.RfShotBufLoad`'s ``s_in`` is a
  ``StreamIFSlave`` with ``has_tlast=True``.  The arena route inserts
  :class:`~waveflow.hw.mem_stream.MemRStream` — a burst engine — to feed a port that was streaming
  anyway.
* **The short-load verdict becomes structural.**  § *The response is not optional* exists because a
  short transfer completes cleanly at the DMA while the buffer sits half loaded.  On a stream that is
  ``TLAST`` before ``nword`` words: the defect is visible **on the data path**, not inferred from a
  completion echo.
* **In Vivado it is one IP.**  MM2S carries header and payload, S2MM carries the verdict, both
  channels of the same AXI DMA.  The arena route needs the kernel's own master wired to a PS HP port
  and ``pynq.allocate().physical_address`` plumbed into the command, whose failure mode is a
  plausible-looking wrong address.

**What was given up, stated so it is not rediscovered as a surprise:** the kernel can no longer
*fetch*.  A resident library of waveforms in DDR, switched by command with no host transfer, is an
``m_axi`` capability, and pulse-to-pulse agility is where it would matter.  Nor can several modules
share one arena without a DMA each.  The escape hatch is scatter-gather DMA with pre-built
descriptors, which is **more** Vivado than the ``m_axi`` it replaces, not less.

**What was NOT given up: the ceiling.**  ``m_axi`` does not let a design exceed the buffer.  Playing
past the shot means a producer refilling while a consumer drains — a live reader and a live writer,
which is the concurrency problem :mod:`waveflow.hw.rf_tx_stream` and ``plans/rf_samp_new.md``'s
credit/ack/margin machinery exist for.  Even the BRAM-less version (burst from DDR straight at the
converter) does not escape it: variable DDR latency against a hard grid deadline forces a prefetch
FIFO, and managing that FIFO's occupancy *is* a streaming buffer.  **The transport choice does not
move the boundary in** ``docs/guide/rf/choosing.md`` **— that boundary is concurrency.**

The in-band *address* argument survives untouched, and still forbids a control register: per
``plans/design_cut.md`` ``BFM_DUALS["axilite_slave"].model is None``, so a design taking its command
from a host-written register could not be XSI-lowered at all.

**Nothing here modifies Stage A.**  ``RfShotBufLoad`` and ``RfShotBufRead`` are used exactly as they
were built and gated; the command layer sits *in front of* the loader.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar

import numpy as np

from waveflow.hw.bram import BramIF, T2pBram, word_element
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
from waveflow.hw.mem_stream import KernelTask
from waveflow.hw.rf_relayout import RfRelayoutToSlots
from waveflow.hw.rf_samp_buf import IDX_BW
from waveflow.hw.rf_shot_buf import (
    BUF_DEPTH,
    SHOT_WORDS,
    WORD_BW,
    RfShotBufLoad,
    RfShotBufRead,
    ShotPhase,
)
from waveflow.simulation.simobj import ProcessGen

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
#: ``ap_done``.  :class:`ShotTxLoad` is an ``hls::task``: **there is no loop to break**, because the
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
#: hand over); :class:`~waveflow.hw.rf_shot_loop.RfShotTxLoop` implements only it.  The code lives
#: here because the *header* is one schema and a second vocabulary for one opcode would be worse.
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
#: (:attr:`~waveflow.hw.rf_shot_buf.RfShotBuf.nword`) is the single source for the length; a command
#: that disagreed is refused rather than truncated, because a truncated waveform is data of the wrong
#: duration and plays as a quieter, shorter signal.
SHOT_WRONG_LEN = 2
#: The header arrived while a shot was playing.  Refused **at the command**, before a word is taken,
#: rather than asserted after the fact by :class:`~waveflow.hw.rf_shot_buf.ShotPhase`.
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
    once on ``RfShotBuf.nword``; a command that restated it would be a second source that could
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


# ---------------------------------------------------------------------------
# The loader: a header, a verdict, and the payload on its way to the buffer
# ---------------------------------------------------------------------------

@dataclass
class ShotTxLoad(FreeRunMod):
    r"""Read a header, decide, forward the payload, answer — exactly one response per header.

    Sits **in front of** :class:`~waveflow.hw.rf_shot_buf.RfShotBufLoad`, which is unchanged and
    unaware::

        s_in --[ShotTxHdr | w0 w1 ... TLAST]--> ShotTxLoad --pay--> RfShotBufLoad --> BRAM
                                                    | ^ done
                                                    | +---------------------- ShotTxPlay
                                                    +--rep--> ShotTxPlay
                                                    +--resp--> the host

    **The frame boundary is the mechanism, not a convenience.**  ``s_in`` is a
    :class:`~waveflow.hw.interface.FramedStreamIFSlave`, so the kernel has a real ``TLAST`` pin.  A
    payload word and a header word are the same 64 bits, so without it there is no in-band way to
    say *that was the end* — a host that sent fewer words than it declared would simply stall the
    buffer's counted loop, and a hang is indistinguishable from a deadlock.  With it, a short
    transfer is a **verdict on the data path** rather than something inferred from a completion echo
    that never arrives.

    Four refusals, and each is a different repair
    ---------------------------------------------
    :data:`SHOT_ZERO_LEN` and :data:`SHOT_WRONG_LEN` are faults in the *command*, decided from the
    header alone and answered **before a single payload word is taken**.  :data:`SHOT_BUSY` is a
    fault in the *timing* — a load arriving while a shot is playing — and it is refused rather than
    queued, because accepting it would overwrite the memory under the reader, which is precisely the
    overlap :class:`~waveflow.hw.rf_shot_buf.ShotPhase` exists to make unreachable.
    :data:`SHOT_SHORT` is the one verdict that cannot be reached from the header: it is what the
    *stream* turned out to be, and ``nsamp_loaded`` is the number a DMA cannot produce.

    A refused header still drains its payload
    -----------------------------------------
    The law :mod:`waveflow.hw.rf_tx_stream`'s loader states, and it is the same law here: words left
    on the wire become the *next* header, and every command after that is garbage for reasons that
    look nothing like the cause.  The drain is a counted loop that runs whatever the verdict; only
    the ``pay_out`` write is conditional.

    An accepted-but-short shot is PADDED
    ------------------------------------
    :class:`~waveflow.hw.rf_shot_buf.RfShotBufLoad`'s inner loop is counted — ``nword`` words, no
    early exit — which is exactly why it reaches II=1 where the streaming buffer cannot, and it is
    not this module's to change.  So a short frame is completed with zeros: the buffer fills, emits
    its one token, and the design stays live.  The zeros are never played, because a short shot is
    handed a repeat count of **zero**: the token still has to be consumed (nothing else will take
    it), but half a waveform must not reach the converter.  That is why the verdict and the repeat
    count travel together.
    """

    cpp_kernel_name: ClassVar[str | None] = "shot_tx_load"

    #: Word width in bits — the stream's, the buffer's, and the converter's.  One number, because the
    #: 64-bit choice (``plans/adc_model.md`` § *Take 64 bits, not 56*) makes the whole path one width.
    bitwidth: HwParam[int] = WORD_BW
    #: Words in one shot.  **Build-time structure**, and the single source: the header's ``nsamp`` is
    #: what the host *believes*, and catching the two disagreeing is what :data:`SHOT_WRONG_LEN` is.
    nword: HwParam[int] = SHOT_WORDS
    #: Samples one word carries — what turns a word count into the ``nsamp`` a host speaks in.
    samp_per_word: HwParam[int] = 4
    clk: Clock = field(default_factory=lambda: Clock(freq=250e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        w, nw, spw = int(self.bitwidth), int(self.nword), int(self.samp_per_word)
        if nw < 1:
            raise ValueError(f"a shot is {nw} words; there is nothing to load")
        if spw < 1 or w % spw:
            raise ValueError(
                f"a {w}-bit word cannot carry {spw} samples without one straddling a slot")
        if nw * spw >= (1 << IDX_BW):
            raise ValueError(
                f"a shot of {nw} words x {spw} samples is {nw * spw} samples, which does not fit "
                f"the {IDX_BW}-bit nsamp / nsamp_loaded fields. A verdict that wrapped would report "
                f"a short load as a correct one.")
        #: The host's port: header **and** payload, one frame, ``TLAST`` at the end.
        self.s_in = FramedStreamIFSlave(sim=self.sim, name=f"{self.name}_s_in", bitwidth=w,
                                        has_tlast=True)
        #: One token per completed play-set, from :class:`ShotTxPlay`.  The only thing that clears
        #: busy, and the only reason this module has an input other than the host's.
        self.done_in = StreamIFSlave(sim=self.sim, name=f"{self.name}_done", bitwidth=w,
                                     has_tlast=True)
        #: The payload, on its way to the unmodified Stage A loader.
        self.pay_out = StreamIFMaster(sim=self.sim, name=f"{self.name}_pay", bitwidth=w,
                                      has_tlast=True)
        #: How many times to play what was just loaded — **zero** when the shot is not playable.
        self.rep_out = StreamIFMaster(sim=self.sim, name=f"{self.name}_rep", bitwidth=w,
                                      has_tlast=True)
        #: The verdict.  A boundary port with its own ``TLAST`` so a host reading it through an AXI
        #: DMA S2MM channel learns where one response ends without being told how long it is.
        self.resp_out = FramedStreamIFMaster(sim=self.sim, name=f"{self.name}_resp", bitwidth=w,
                                             has_tlast=True)
        for ep in (self.s_in, self.done_in, self.pay_out, self.rep_out, self.resp_out):
            self.add_endpoint(ep)
        #: Shots accepted and not yet finished playing.  Sim-only bookkeeping — the C++ twin holds
        #: the same one bit in a ``static`` — and it is what :data:`SHOT_BUSY` reads.
        self.busy = 0
        #: ``(tid, status, nsamp_loaded)`` for every response, in the order they went out.  A run's
        #: verdicts are the thing this module exists to produce, so a gate reads them here rather
        #: than re-deserializing the stream.
        self.resps: list[tuple[int, int, int]] = []

    # -- geometry ----------------------------------------------------------------------------

    @property
    def nsamp_shot(self) -> int:
        """Samples in a full shot — the one value ``ShotTxHdr.nsamp`` may carry."""
        return int(self.nword) * int(self.samp_per_word)

    def kernel_task(self) -> KernelTask:
        return KernelTask("shot_tx_load_task", "shot_tx_load_task.h",
                          ("s_in", "done_in", "pay_out", "rep_out", "resp_out"),
                          template_args=(int(self.bitwidth), int(self.nword),
                                         int(self.samp_per_word)))

    # -- the pysim twin ----------------------------------------------------------------------

    def _verdict(self, hdr) -> int:
        """The header-only refusals, in the order the C++ body tests them.

        **Malformed before transient**, which is :mod:`waveflow.hw.rf_tx_stream`'s order and for its
        reason: a command that is wrong *and* badly timed should be told the thing it can fix.  Retry
        repairs a :data:`SHOT_BUSY`; nothing repairs a length that does not fit the buffer.
        """
        if int(hdr.nsamp) == 0:
            return SHOT_ZERO_LEN
        if int(hdr.nsamp) != self.nsamp_shot:
            return SHOT_WRONG_LEN
        if self.busy:
            return SHOT_BUSY
        return SHOT_LOADED

    def run_iter(self) -> ProcessGen[None]:
        """One firing is one frame, which is one iteration of the C++ body.

        ``get()`` with no count returns **the whole burst**, and a burst is a ``TLAST``-delimited
        frame — so the pysim read of "the header, then words until last" is one call, and the two
        backends see the same boundary rather than two encodings of it.  Everything after that is
        what ``shot_tx_load_task.h`` does, in the same order.
        """
        w, nw = int(self.bitwidth), int(self.nword)
        frame = np.asarray((yield from self.s_in.get()), dtype=np.uint64).ravel()

        hn = ShotTxHdr.nwords_per_inst(w)
        # The schema's own deserializer, never a field walk: the layout has one author and the
        # generated C++ reads it through the same statement.
        hdr = ShotTxHdr().deserialize(frame[:hn], word_bw=w)
        payload = frame[hn:]

        # The done token is harvested AFTER the header has arrived, so `busy` is as fresh as it can
        # be when read.  Non-blocking: a play still running is the answer, not a reason to wait.
        if (yield from self.done_in.get_nb(nwords_max=1)) is not None:
            self.busy = 0

        if int(hdr.opcode) == SHOT_END:
            # A fence.  No payload to drain (the frame is the header), nothing to change, and its
            # response is the proof that everything ahead of it has been processed.
            yield from self._answer(hdr, SHOT_LOADED, 0)
            return

        status = self._verdict(hdr)
        accept = status == SHOT_LOADED
        # One counted pass does all three jobs: forward, drain, pad.  `payload` may be shorter than
        # the shot (a short frame), longer (a malformed one, whose tail is dropped rather than left
        # to become the next header) or exact.
        took = min(int(payload.size), nw)
        if accept:
            for i in range(nw):
                val = int(payload[i]) if i < took else 0
                yield from self.pay_out.write(np.array([val], dtype=np.uint64))
            if took < nw:
                status = SHOT_SHORT
            # A shot that is not playable is loaded and then not played: the token the buffer emits
            # still has to be consumed, so the play-set is zero repeats rather than absent.
            nrep = int(hdr.nrepeat) if status == SHOT_LOADED else 0
            yield from self.rep_out.write(np.array([nrep], dtype=np.uint64))
            self.busy = 1
        yield from self._answer(hdr, status, took * int(self.samp_per_word) if accept else 0)

    def _answer(self, hdr, status: int, nsamp_loaded: int) -> ProcessGen[None]:
        """Exactly one :class:`ShotTxResp` per header — see ``plans/rf_shot_buf.md`` § *Why no
        ``has_response`` flag*."""
        r = ShotTxResp()
        r.tid, r.status, r.nsamp_loaded = int(hdr.tid), int(status), int(nsamp_loaded)
        self.resps.append((int(hdr.tid), int(status), int(nsamp_loaded)))
        yield from self.resp_out.write(r)


# ---------------------------------------------------------------------------
# The player: nrepeat plays, and the only thing that knows when they are over
# ---------------------------------------------------------------------------

@dataclass
class ShotTxPlay(FreeRunMod):
    r"""Turn one loaded shot into ``nrepeat`` plays, and say when the last one has gone out.

    It sits on **both** the token channel and the sample path, and needs both::

        RfShotBufLoad --rdy--> ShotTxPlay --rdy--> RfShotBufRead --> RfRelayoutToSlots
                                   ^ rep                                     |
                              ShotTxLoad <--- done --- ShotTxPlay <--- samp --+
                                                            |
                                                            +--> the converter

    **Why the repeat is not simply more tokens.**  ``RfShotBufRead`` plays one shot per ``rdy`` token
    and is RTL-gated as it stands, so the repeat has to come from something in front of it.  That
    something could have been :class:`ShotTxLoad` writing ``nrepeat`` tokens — but the token channel
    is depth 1, so the write of token *k+1* returns when the reader **starts** play *k*, not when it
    finishes.  A loader that answered "done" there would clear busy while the last play was still
    coming out of the memory, and the next load would overwrite it mid-play.  Sitting on the sample
    path is what makes completion **exact**: the ``done`` token is written after the last word of the
    last play has been handed on, and there is nothing left to be early about.

    **Nothing here is a schedule.**  There is no grid arithmetic, no deadline and no slot: the
    converter back-pressures, the relayout back-pressures, this task stalls, and the memory holds.
    That is the whole of the never-miss-a-deadline obligation on the shot design — once a play has
    started, a BRAM read at II=1 can always supply a word per cycle, so the only reachable underruns
    are the ones before the first word arrives.  Compare :mod:`waveflow.hw.rf_tx_stream`, which needs
    an absolute slot grid, an ack channel and a lateness verdict, because *its* source can genuinely
    fall behind.

    **No ``static``, anywhere.**  A play-set lives entirely inside one firing, so there is no state
    to carry across firings and nothing for the reset trap to catch (``reference-hls-task-reset-trap``:
    an ``hls::task`` that WRITES before it READS advances during reset).  This body's first act is a
    blocking read, twice over.
    """

    cpp_kernel_name: ClassVar[str | None] = "shot_tx_play"

    bitwidth: HwParam[int] = WORD_BW
    #: Words in one shot — the inner loop's trip count, and the same build-time number
    #: :class:`~waveflow.hw.rf_shot_buf.RfShotBufRead` emits per token.
    nword: HwParam[int] = SHOT_WORDS

    # -- two modelling inputs, and neither is hardware -------------------------------------------
    #
    # Both exist because pysim's quantum on the converter edge is a *block*, and both are the same
    # accommodation :class:`~waveflow.hw.rf_samp_buf_tx.RfSampBufPlayer` documents at length.  They
    # are plain fields rather than HwParams deliberately: they reach no template argument, because
    # the RTL body writes one word per beat and knows nothing about either.

    #: Words per pysim output burst.  ``Rfdc``'s DAC process consumes a whole ``blksize``-sample
    #: burst per event and refuses a partial one, so the twin must hand it exactly that.  ``1`` is
    #: the honest default (one word, one write); a testbench wiring this to a converter sets it to
    #: ``blksize // samp_per_word``.  Must divide :attr:`nword`, so a play is a whole number of
    #: blocks and a play boundary lands on a block boundary.
    blk_words: int = 1
    #: **Words per second the DAC consumes** — ``samp_rate / samp_per_word`` — or ``None`` to run at
    #: the fabric's rate alone.
    #:
    #: In RTL this task is paced by ``TREADY`` and needs nothing: it writes a word, the 2-deep
    #: boundary port fills, and it waits for the converter to take one.  That back-pressure **is** the
    #: whole scheduling story of the shot design, which is why the body has no grid arithmetic in it.
    #: pysim has no such back-pressure for a burst write (``StreamIF`` routes intra-burst overflow to
    #: a counter rather than blocking — see ``docs/guide/rf/rfdc/fidelity.md``), so the metronome has
    #: to be handed over instead.  Left unset the playout runs at the fabric's rate, the converter is
    #: never the bottleneck, and the one property this design claims — that it keeps a DAC fed — is
    #: not being tested at all.
    dac_word_rate: float | None = None
    clk: Clock = field(default_factory=lambda: Clock(freq=250e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        w, nw, bw = int(self.bitwidth), int(self.nword), int(self.blk_words)
        if nw < 1:
            raise ValueError(f"a shot is {nw} words; there is nothing to play")
        if bw < 1 or nw % bw:
            raise ValueError(
                f"blk_words={bw} does not divide a {nw}-word shot. pysim hands the converter one "
                f"block per event, so a shot that is not a whole number of blocks would end "
                f"mid-block and the next play would start inside one.")
        self.rep_in = StreamIFSlave(sim=self.sim, name=f"{self.name}_rep", bitwidth=w,
                                    has_tlast=True)
        self.rdy_in = StreamIFSlave(sim=self.sim, name=f"{self.name}_rdy_in", bitwidth=w,
                                    has_tlast=True)
        self.rdy_out = StreamIFMaster(sim=self.sim, name=f"{self.name}_rdy_out", bitwidth=w,
                                      has_tlast=True)
        self.samp_in = StreamIFSlave(sim=self.sim, name=f"{self.name}_samp_in", bitwidth=w,
                                     has_tlast=True)
        self.samp_out = StreamIFMaster(sim=self.sim, name=f"{self.name}_samp_out", bitwidth=w,
                                       has_tlast=True)
        self.done_out = StreamIFMaster(sim=self.sim, name=f"{self.name}_done", bitwidth=w,
                                       has_tlast=True)
        for ep in (self.rep_in, self.rdy_in, self.rdy_out, self.samp_in, self.samp_out,
                   self.done_out):
            self.add_endpoint(ep)
        #: Plays completed over the whole run, and words handed on.  Counted rather than inferred:
        #: a run whose player never fired is a run that proved nothing, and a word count that is not
        #: ``n_plays * nword`` is a truncated play the sample data alone can hide.
        self.n_plays = 0
        self.n_words = 0
        #: The pysim rate grid: when the first block went out, and how many have.  Sim-only, and
        #: ``None`` until the first play-set starts (see :meth:`run_iter`).
        self._t0: float | None = None
        self._blocks = 0

    def kernel_task(self) -> KernelTask:
        return KernelTask("shot_tx_play_task", "shot_tx_play_task.h",
                          ("rep_in", "rdy_in", "rdy_out", "samp_in", "samp_out", "done_out"),
                          template_args=(int(self.bitwidth), int(self.nword)))

    def run_iter(self) -> ProcessGen[None]:
        """One firing is one play-set: the repeat count, the shot's token, then ``nrepeat`` plays.

        **Word-granular on the way in, block-shaped on the way out.**  ``samp_in`` is read one word
        per ``get`` because that is what the stage upstream writes — a pysim slave dequeues a whole
        burst per ``get`` and ``nwords_max`` *discards* the remainder, so a multi-word read here
        would be one pysim firing against ``nword`` RTL firings and the two backends would be running
        different designs (``examples/bram_access`` spells this out).  What is *shaped* is only the
        handover to the converter: :attr:`blk_words` words go out as one burst, because pysim's
        quantum on that edge is a block.

        The rate is charged **per block on an absolute grid**, never as a relative timeout.  A
        relative wait restarts from wherever ``now`` happens to be when the body finishes, so
        everything the body yielded for is added to the period and never given back — the defect
        ``RFSampIF``'s metronome exists to avoid, and the one that made ``rf_samp_buf_tx``'s player
        slip a whole block every fourth firing.
        """
        nw, bw = int(self.nword), int(self.blk_words)
        rep = yield from self.rep_in.get(nwords_max=1)
        nrep = int(np.asarray(rep, dtype=np.uint64).ravel()[0])
        yield from self.rdy_in.get(nwords_max=1)          # the shot is in the memory
        if self._t0 is None:
            # The grid's origin: the instant the FIRST play-set started handing words over.  Set
            # here rather than at construction, because a converter's grid begins when the design
            # has something to put on it, and anchoring at t=0 would charge the load time twice.
            self._t0 = self.now
        for _ in range(nrep):
            yield from self.rdy_out.write(np.array([1], dtype=np.uint64))
            for _ in range(nw // bw):
                out = np.empty(bw, dtype=np.uint64)
                for k in range(bw):
                    word = yield from self.samp_in.get(nwords_max=1)
                    out[k] = int(np.asarray(word, dtype=np.uint64).ravel()[0])
                    self.n_words += 1
                # Hand off FIRST, then charge: charging before the write would make every block
                # arrive one period late, so the player and the converter would serialize rather
                # than overlap.
                yield from self.samp_out.write(out)
                self._blocks += 1
                if self.dac_word_rate:
                    deadline = self._t0 + self._blocks * (bw / float(self.dac_word_rate))
                    yield self.timeout(max(0.0, deadline - self.now))
            self.n_plays += 1
        yield from self.done_out.write(np.array([1], dtype=np.uint64))


# ---------------------------------------------------------------------------
# The composite: a command layer, the Stage A buffer, and the converter's packing
# ---------------------------------------------------------------------------

@dataclass
class RfShotTx(FreeRunMod):
    r"""The whole transmitter as one design scope: load a waveform once, play it ``nrepeat`` times.

    Five ``hls::task``\ s and one memory beside them::

        s_in --> ShotTxLoad --pay--> RfShotBufLoad --> [ BRAM ] --> RfShotBufRead
                  |  |  ^                   |                            |
             resp_out |  |                 rdy                          dense
                      |  |                  v                            v
                      | done            ShotTxPlay --rdy-------> RfRelayoutToSlots
                      |  ^                  ^                            |
                      +-rep-> ShotTxPlay <--samp---------------------- (slots)
                                  |
                                  +--> samp_out --> Rfdc.tx_streams[0]

    (``ShotTxPlay`` appears twice only because the diagram is flat: it is one task, and it is the
    task the converter back-pressures.)

    **The Stage A pair is instantiated, not nested**, and that is forced rather than preferred.
    :class:`~waveflow.hw.rf_shot_buf.RfShotBuf` owns its ``rdy`` channel as an *internal* edge, so a
    composite that used it whole could not put anything on that wire — and the repeat is exactly a
    thing on that wire.  So this composite wires
    :class:`~waveflow.hw.rf_shot_buf.RfShotBufLoad`, :class:`~waveflow.hw.rf_shot_buf.RfShotBufRead`
    and the memory itself, using all three **exactly as Stage A built and gated them**: not one line
    of ``rf_shot_buf.py`` or ``rf_relayout.py`` changes, and the numbers those gates recorded still
    describe the same RTL.

    **Where the relayout goes, and why the player is last.**  The buffer holds *dense* words — the
    logic-side format, four 14-bit samples packed at 14-bit stride in a 64-bit beat — because that is
    what a host can write without knowing anything about justification (``plans/adc_model.md``
    § *The logic-side interface*, option 2).  The converter wants slots, so
    :class:`~waveflow.hw.rf_relayout.RfRelayoutToSlots` sits between them and the buffer owns the
    converter's packing: ``justify`` can change and nothing upstream of that stage moves.

    It goes **before** the player rather than after, and that is a modelling constraint made
    structural.  The last stage on this chain is the one the converter back-pressures, and it is
    therefore the one that has to be paced in pysim — ``Rfdc``'s DAC process consumes a whole
    ``blksize`` burst per event and refuses a partial one, while ``RfRelayoutToSlots`` writes one
    word per firing and is RTL-gated as it stands.  Putting the player last is what lets
    :attr:`ShotTxPlay.blk_words` shape that handover without touching Stage A.  At RTL the order is
    immaterial (both stages are II=1 pass-throughs), which is what makes it free to choose on the
    modelling side.

    **What is absent is the lesson.**  There is no credit channel, no ack, no progress pointer, no
    ``MARGIN``, no slot arithmetic and no lateness verdict — every mechanism ``plans/rf_samp_new.md``
    spends its length on.  The only reverse traffic in the whole diagram is the ``done`` token, and
    it is not arbitration: it is the answer to *may I overwrite the memory yet*, which exists because
    a shot buffer's reader and writer are **never** live at the same time.  ``docs/guide/rf/choosing.md``
    divides the two buffer classes by concurrency, and this diagram is what that division looks like.
    """

    cpp_kernel_name: ClassVar[str | None] = "rf_shot_tx"
    potential_targets: ClassVar[frozenset[str]] = frozenset({COMPOSITE_KERNEL})

    #: Word width in bits — one number for the host port, the memory, the player and the converter.
    bitwidth: HwParam[int] = WORD_BW
    #: Samples one word carries.
    samp_per_word: HwParam[int] = 4
    #: Memory depth in **WORDS** (a power of two: the address wrap is a mask).
    depth: HwParam[int] = BUF_DEPTH
    #: Words in one shot.  ``<= depth``.
    nword: HwParam[int] = SHOT_WORDS
    #: Bits the effective sample sits above the bottom of its converter slot —
    #: :meth:`~waveflow.hw.rfdc_samp_word.RfdcSampWord.justify_shift`.  **0 makes the last stage the
    #: identity**, which is every configuration in the repo but the 4x2 preset, so a build that leaves
    #: it at 0 is measuring a pair of wires.
    shift: HwParam[int] = 2
    #: Words per pysim output burst on the converter-facing port, and the DAC's word rate — both
    #: :class:`ShotTxPlay`'s modelling inputs, passed through.  See :attr:`ShotTxPlay.blk_words` and
    #: :attr:`ShotTxPlay.dac_word_rate`; neither reaches the hardware.
    blk_words: int = 1
    dac_word_rate: float | None = None
    clk: Clock = field(default_factory=lambda: Clock(freq=250e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        w, d, nw = int(self.bitwidth), int(self.depth), int(self.nword)
        spw, sh = int(self.samp_per_word), int(self.shift)
        if d & (d - 1):
            raise ValueError(f"buffer depth must be a power of two (got {d}): the wrap is a mask")
        if not 1 <= nw <= d:
            raise ValueError(
                f"a shot is {nw} words but the buffer holds {d}: a shot longer than the memory is "
                f"not a shot, it is a stream, and streaming is what waveflow.hw.rf_tx_stream is for")

        self.load = ShotTxLoad(sim=self.sim, name=f"{self.name}_load", bitwidth=w, nword=nw,
                               samp_per_word=spw, clk=self.clk)
        self.buf_load = RfShotBufLoad(sim=self.sim, name=f"{self.name}_buf_load", bitwidth=w,
                                      depth=d, nword=nw, clk=self.clk)
        self.play = ShotTxPlay(sim=self.sim, name=f"{self.name}_play", bitwidth=w, nword=nw,
                               blk_words=int(self.blk_words),
                               dac_word_rate=self.dac_word_rate, clk=self.clk)
        self.buf_read = RfShotBufRead(sim=self.sim, name=f"{self.name}_buf_read", bitwidth=w,
                                      depth=d, nword=nw, clk=self.clk)
        self.relayout = RfRelayoutToSlots(sim=self.sim, name=f"{self.name}_to_slots", bitwidth=w,
                                          n_slot=spw, shift=sh, clk=self.clk)
        # add_comp order is emit order, and it is the DATA-FLOW order: command layer, buffer write,
        # the token stage, buffer read, the converter's packing, and the player last because it is
        # the stage the converter back-pressures.
        for c in (self.load, self.buf_load, self.buf_read, self.relayout, self.play):
            self.add_comp(c)

        # -- the internal channels.  Each depth is a statement, not a default. -------------------
        #
        # The two token channels are depth 1 because there is exactly one token in flight by
        # construction: `done` cannot accumulate (a second load is refused until it arrives) and a
        # `rdy` cannot either (the reader takes one before doing anything).  A deeper queue could
        # only hold a token for a shot that has already been overwritten, which is the state
        # ShotPhase refuses.  The word channels are depth 2 -- the HLS default for a top argument and
        # enough for a producer and a consumer to overlap by one beat, which is all an II=1 chain
        # needs.
        for nm, master, slave, depth in (
                ("pay", self.load.pay_out, self.buf_load.s_in, 2),
                ("rep", self.load.rep_out, self.play.rep_in, 1),
                ("rdy_load", self.buf_load.rdy_out, self.play.rdy_in, 1),
                ("rdy_play", self.play.rdy_out, self.buf_read.rdy_in, 1),
                ("dense", self.buf_read.s_out, self.relayout.s_in, 2),
                ("samp", self.relayout.s_out, self.play.samp_in, 2),
                ("done", self.play.done_out, self.load.done_in, 1)):
            ifc = StreamIF(name=f"{self.name}_{nm}_if", sim=self.sim, clk=self.clk, bitwidth=w,
                           depth=depth)
            ifc.bind(ep_name="master", endpoint=master)
            ifc.bind(ep_name="slave", endpoint=slave)
            self.add_if(ifc)

        # `mem`, not `buf`: the attribute name becomes the Verilog INSTANCE name and `buf` is a
        # primitive gate name, which the wrapper emitter refuses by name rather than letting xvlog
        # fail on a syntax error that mentions no Python.
        self.mem = T2pBram(sim=self.sim, name=f"{self.name}_mem",
                           element_type=word_element(w), nelem=d)
        self.add_rtl_mod(self.mem)
        for nm, master, slave in (("bufw", self.buf_load.buf_w, self.mem.wr_port),
                                  ("bufr", self.buf_read.buf_r, self.mem.rd_port)):
            # A BramIF goes in add_rtl_if and NEVER add_if: the walks that derive channels and
            # boundary ports read the add_if registry, and a BramIF in it would make the kernel's
            # memory ports disappear into a FIFO that does not exist.
            rif = BramIF(name=f"{self.name}_{nm}_if", sim=self.sim)
            rif.bind(ep_name="master", endpoint=master)
            rif.bind(ep_name="slave", endpoint=slave)
            self.add_rtl_if(rif)

        #: One :class:`~waveflow.hw.rf_shot_buf.ShotPhase` for both halves of the buffer — the
        #: assertion spans them, so it cannot live in either.  The same wiring
        #: :class:`~waveflow.hw.rf_shot_buf.RfShotBuf` does, restated here because this composite
        #: instantiates the two tasks rather than the packaged pair.
        self.phase = ShotPhase()
        self.buf_load.phase = self.phase
        self.buf_read.phase = self.phase

        #: ``add_comp`` x ``add_endpoint`` order with every internally-bound endpoint removed.  The
        #: two ``buf_*`` entries are ports of the KERNEL, joined to the memory inside the wrapper.
        self.boundary = ["s_in", "resp_out", "buf_w", "buf_r", "samp_out"]

        # Convenience refs for testbenches — the boundary endpoints live on the children.
        self.s_in = self.load.s_in
        self.resp_out = self.load.resp_out
        self.samp_out = self.play.samp_out

    # -- geometry, read off the graph rather than restated -------------------------------------

    @property
    def nsamp_shot(self) -> int:
        """Samples in one shot — what a host's ``ShotTxHdr.nsamp`` must equal."""
        return int(self.nword) * int(self.samp_per_word)

    @property
    def nsamp_held(self) -> int:
        """Samples the memory holds.  **100% payload**: no headroom for data in flight, because
        there is none — see :attr:`~waveflow.hw.rf_shot_buf.RfShotBuf.nsamp_held`."""
        return int(self.depth) * int(self.samp_per_word)

    @property
    def is_identity(self) -> bool:
        """``True`` when the last stage's re-layout does nothing — see
        :attr:`~waveflow.hw.rf_relayout.RfRelayout.is_identity`.  A gate should assert it is
        ``False``, or it is measuring a pair of wires rather than the conversion."""
        return int(self.shift) == 0

    @classmethod
    def for_word(cls, word, *, depth: int = BUF_DEPTH, nword: int = SHOT_WORDS, **kwargs):
        """Build the whole transmitter from the converter's **word type** — the single place the four
        integers are derived.

        The same reading :meth:`~waveflow.hw.rf_shot_buf.RfShotBuf.for_word` does, extended by the
        one number the relayout needs.  A type cannot be an ``HwParam``
        (``HwModule.__post_init__`` wraps every one in ``HwParamValue(int(value))``), so what
        survives the call is integers and this classmethod is where they come from.
        """
        from waveflow.hw.rf_relayout import check_geometry, slots_per_word
        from waveflow.hw.rfdc_samp_word import RfdcSampWord

        if not (isinstance(word, type) and issubclass(word, RfdcSampWord)):
            raise TypeError(
                f"RfShotTx.for_word() takes the converter's WORD TYPE — the packing convention, not "
                f"a width. Got {word!r}. Build one with RfdcSampWord.specialize(...) or a board "
                f"preset such as Rfsoc4x2SampWord.specialize(samp_per_word=4).")
        check_geometry(word)
        return cls(bitwidth=int(word.bitwidth), samp_per_word=slots_per_word(word),
                   depth=int(depth), nword=int(nword), shift=int(word.justify_shift()), **kwargs)

    # -- counters ------------------------------------------------------------------------------

    @property
    def resps(self) -> list[tuple[int, int, int]]:
        """``(tid, status, nsamp_loaded)`` for every response, in the order they went out."""
        return list(self.load.resps)

    @property
    def n_plays(self) -> int:
        """Plays the player finished."""
        return int(self.play.n_plays)

    def assert_played(self, n_plays: int) -> None:
        """After a run: the player played *n_plays* shots, whole ones, and the buffer's two phases
        never overlapped.

        Three claims a byte comparison does not make.  A playout that stopped mid-shot has the right
        words in the right order for as far as it got — the failure this repo keeps meeting — so the
        word count is checked against ``n_plays * nword`` rather than assumed from it.
        """
        got, want_words = int(self.play.n_plays), int(n_plays) * int(self.nword)
        if got != int(n_plays):
            raise AssertionError(
                f"{type(self).__name__} '{self.name}': played {got} shots, expected {int(n_plays)}. "
                f"Responses so far: {self.resps}")
        if int(self.play.n_words) != want_words:
            raise AssertionError(
                f"{type(self).__name__} '{self.name}': {got} plays handed on "
                f"{int(self.play.n_words)} words, not {want_words}. A play that stopped part way "
                f"carries the right samples as far as it got, so only the count says so.")
        self.assert_phases_separated()

    def assert_phases_separated(self) -> None:
        """Restate, after a run, what :class:`~waveflow.hw.rf_shot_buf.ShotPhase` refused during it.

        A guard that never fired is not evidence that the invariant held — it is evidence that
        *something* ran.  Same statement as
        :meth:`~waveflow.hw.rf_shot_buf.RfShotBuf.assert_phases_separated`, reached through this
        composite's own phase object.
        """
        p = self.phase
        if p.n_written == 0 or p.n_read == 0:
            raise AssertionError(
                f"{type(self).__name__} '{self.name}': the phase guard cannot have proved anything — "
                f"{p.n_written} words were written and {p.n_read} read. A run in which one side "
                f"never moved has not exercised the separation it claims to keep.")
        if p.writing or p.reading:
            raise AssertionError(
                f"{type(self).__name__} '{self.name}': the run ended mid-phase "
                f"(writing={p.writing}, reading={p.reading}). A shot that was started and not "
                f"finished leaves the memory holding half a signal, which is invisible from a word "
                f"count.")
