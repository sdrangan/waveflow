"""rf_samp_buf_tx.py — ``RfSampBufTx``: the sample buffer that feeds a converter's DAC path.

**The mirror of :mod:`waveflow.hw.rf_samp_buf`, and the asymmetry is the whole design.**

RX exists because **an ADC cannot be back-pressured**.  Its ingress may never *refuse a write*, and
it fails by **overrunning** — samples arrive and there is nowhere to put them.

TX has the opposite obligation.  **A DAC consumes a word every sample period whether or not you have
one for it**, so the player may never *miss a deadline*, and it fails by **underrunning** — a sample
period comes due and the slot was never filled.  Three things follow, and they are the design:

- **The player is free-running on the metronome, not demand-driven.**  It emits on the grid, always.
  There is no command on that side and nothing to wait for.
- **It never blocks waiting for data.**  If the loader has not filled the slot, it emits what is
  there — stale from the previous lap, or the buffer's initial contents — and counts an underrun.
  Waiting for the loader is the one thing it may not do.
- **The buffer is circular, not a FIFO**, and there are **no dropped samples** on the playout side.
  A slot is either right or stale, and the counter is what tells them apart.

::

    s_in --> [loader] --BramIF(write)--> T2pBram --BramIF(read)--> [player] --> s_out
      |          ^  |                                                 |  ^
      |          |  +------------- fill channel (wr) -----------------+  |
      |          +---------------- play channel (rd) --------------------+
      +-- TxCmd, then its payload IN-BAND behind it          [loader] --> s_resp

**In-band payload, one port.**  The samples arrive on the *same* stream as the command, immediately
behind it — the ``mem_copy`` / interleaver framed shape, which ``plans/adc_model.md`` makes the
primary variant because it is XSI-proven.  There is no ``data_addr`` and no ``m_axi``.

**Two progress channels, not one, and they point opposite ways.**  In RX the writer tells the reader
where it is.  Here both directions are needed: the player tells the loader where it has played (so
the loader knows what is safe to overwrite and what has already gone out), and the loader tells the
player how far it has filled (so the player can tell a fresh slot from a stale one).  Both are depth
1 and both are written non-blockingly, for the reason RX's is: only the newest position means
anything.

**Two buffers, not one shared with RX.**  ``plans/adc_model.md`` recommends it and the code agrees:
"never refuse a write" and "never miss a deadline" are different contracts, they need the progress
channels to point in opposite directions, and a shared ``T2pBram`` has exactly one write port and one
read port — which the two paths would then have to arbitrate for. Sharing would add a contention
question to two modules whose entire purpose is to have none.

**Geometry, alignment and the wrapping counter are shared with RX**, deliberately: ``IDX_BW``,
:func:`~waveflow.hw.rf_samp_buf.sdiff`, :func:`~waveflow.hw.rf_samp_buf.samp_type` and the status
vocabulary are imported rather than restated, so the two directions cannot drift.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar

import numpy as np

from waveflow.hw.bram import BramIF, BramIFMaster, T2pBram
from waveflow.hw.clock import Clock
from waveflow.hw.codegen_targets import COMPOSITE_KERNEL
from waveflow.hw.dataschema import DataList
from waveflow.hw.hw_freerun import FreeRunMod
from waveflow.hw.hw_module import HwParam
from waveflow.hw.interface import StreamIF, StreamIFMaster, StreamIFSlave
from waveflow.hw.mem_stream import KernelTask
from waveflow.hw.rf_samp_buf import (
    BUF_DEPTH,
    HORIZON_MARGIN,
    IDX_BW,
    RF_SAMP_BUF_MISALIGNED,
    RF_SAMP_BUF_OK,
    RF_SAMP_BUF_TOO_LATE,
    WORD_BW,
    IdxField,
    samp_type,
    sdiff,
)
from waveflow.hw.synth import sim_only
from waveflow.simulation.simobj import ProcessGen


class TxCmd(DataList):
    """One playout command: place the payload behind it at sample indices ``[start, start + nsamp)``.

    The mirror of :class:`~waveflow.hw.rf_samp_buf.RxCmd`, field for field, and deliberately so: a
    host that drives both directions writes one struct shape twice rather than learning two.

    ``start`` is in **sample index** — the DAC's own running count, the moment these samples play —
    which is what makes a timestamped hand-off possible at all.  The payload follows **in band**,
    ``ceil(nsamp / samp_per_word)`` words immediately behind the command on the same stream.
    """

    include_filename: ClassVar[str | None] = "tx_cmd.h"
    elements = {
        "tid":   {"schema": IdxField, "description": "transaction id, echoed on the response"},
        "start": {"schema": IdxField, "description": "sample index at which the payload plays"},
        "nsamp": {"schema": IdxField, "description": "samples in the payload behind this command"},
    }


class TxResp(DataList):
    """One response per command — the **counted contract**, mirroring
    :class:`~waveflow.hw.rf_samp_buf.RxResp`.

    ``nloaded`` is the TX counterpart of ``nsent``: how many samples were actually placed.  Without
    it a host cannot tell "your samples arrived after their slot played" from "they are queued and
    will play" — the difference between a bug and a wait, indistinguishable if both look like
    silence.
    """

    include_filename: ClassVar[str | None] = "tx_resp.h"
    elements = {
        "tid":     {"schema": IdxField, "description": "the command's transaction id"},
        "status":  {"schema": IdxField,
                    "description": "0 = OK, 2 = misaligned, 3 = too late (the slot already played)"},
        "nloaded": {"schema": IdxField, "description": "samples actually placed in the buffer"},
    }


#: The schema classes a TX example must run ``DataSchemaStep`` over to get ``tx_cmd.h`` /
#: ``tx_resp.h``.
TX_SCHEMA_CLASSES = [TxCmd, TxResp]


# ---------------------------------------------------------------------------
# The two tasks
# ---------------------------------------------------------------------------

@dataclass
class RfSampBufLoader(FreeRunMod):
    """One ``TxCmd`` in, its in-band payload into the circular buffer, one ``TxResp`` out.

    **This task is allowed to block** — the mirror of
    :class:`~waveflow.hw.rf_samp_buf.RfSampBufCapture`.  Nothing downstream misses a deadline while
    it waits: the player keeps playing whatever the loader is doing.  That freedom is what lets a
    command name a slot in the future and simply *hold* until the buffer has room for it.

    ==============  =========================================  ====================================
    case            condition                                  what happens
    ==============  =========================================  ====================================
    in the future   ``start`` leads ``rd`` by < ``depth*spw``   placed straight away
    too far ahead   ``start`` leads ``rd`` by >= ``depth*spw``  **held** until the player advances
    too late        ``start`` trails ``rd`` (within margin)     refused and counted, never placed
    misaligned      ``start`` or ``nsamp`` not a whole word     refused and counted
    ==============  =========================================  ====================================

    **The frame is drained whatever the verdict.**  A refused command whose payload is left in the
    stream desynchronises every command after it — the next read would take a sample for a ``tid``.
    So the loop always consumes ``ceil(nsamp/spw)`` words and the status only decides whether each is
    stored.  That is the one place this body's shape differs from the RX capture's, which had no
    payload and could simply ``break``.

    **What the margin is for, and why it guards the other test than RX's.**  :attr:`last_rd` is a
    *lower* bound on the player's true position, because the play channel drops rather than stalling
    the player.  So "is there room?" is made harder to pass (safe — this task waits longer) and "has
    it already played?" is made *easier* (unsafe — a slot that has in fact gone out could be
    written).  The margin bounds the unsafe direction, which for TX is the too-late test; in RX it
    was the horizon test.  The unsafe direction moves with the direction of dataflow.
    """

    cpp_kernel_name: ClassVar[str | None] = "rf_samp_buf_loader"

    #: **Fabric cycles per PAYLOAD WORD** — and deliberately *not* called ``fire_cycles``, because
    #: this body has no cycles-per-firing to declare.
    #:
    #: MEASURED, from ``PipelineII`` on ``VITIS_LOOP_99_2`` in
    #: ``rf_samp_buf_loader_task_..._Pipeline_VITIS_LOOP_99_2_csynth.xml``: the payload loop is
    #: pipelined at an **achieved** II of 2 (against a target of 1, which Vitis did not meet — the
    #: achievement is what a cost model may use, the target is a wish).
    #:
    #: **There is no per-firing constant here and this replaces the one that used to be.**  A firing
    #: is one whole command, and the outer ``VITIS_LOOP_85_1`` has a data-dependent trip count, so
    #: the module's overall latency is reported ``undef``.  The previous ``fire_cycles = 2`` was
    #: justified by symmetry with :class:`~waveflow.hw.rf_samp_buf.RfSampBufIngress` — "the same
    #: shape, so the same cost" — and the report refutes the premise: that body is a single-word
    #: firing with a bounded 1-cycle latency, this one is a loop over an unbounded payload.
    #:
    #: **What is still not charged:** the framing — the command read, the response write, and the
    #: outer loop's entry and exit.  That is a per-command overhead the report does not bound, so the
    #: pysim charge is optimistic by it.  It is a constant per command rather than per word, so it
    #: does not distort the per-word rate, only the fixed offset.
    word_cycles: ClassVar[int] = 2

    #: AXIS word width in bits.  Read off the converter's ``axis_bitwidth``.
    bitwidth: HwParam[int] = WORD_BW
    #: Samples carried by one word; ``bitwidth // samp_per_word`` is the sample width.
    samp_per_word: HwParam[int] = 1
    #: Buffer depth in **words** (power of two — the wrap is a bit mask).
    depth: HwParam[int] = BUF_DEPTH
    #: **Samples** of lead a slot must have over the last-known play position to be accepted.  See
    #: the class docstring for which direction it guards.
    horizon_margin: HwParam[int] = HORIZON_MARGIN
    clk: Clock = field(default_factory=lambda: Clock(freq=300e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        w, d, spw = int(self.bitwidth), int(self.depth), int(self.samp_per_word)
        if d & (d - 1):
            raise ValueError(f"buffer depth must be a power of two (got {d}): the wrap is a mask")
        if spw & (spw - 1):
            raise ValueError(
                f"samp_per_word must be a power of two (got {spw}): the sample->word conversion is a "
                f"shift in the never-miss path, and a divide there would cost cycles the DAC does "
                f"not give back")
        samp_type(w, spw)                       # refuses a sample that would straddle a slot
        self.s_in = StreamIFSlave(sim=self.sim, name=f"{self.name}_s_in", bitwidth=w, has_tlast=True)
        self.buf_w = BramIFMaster(sim=self.sim, name=f"{self.name}_buf_w", bitwidth=w, depth=d,
                                  access="write")
        self.rd_in = StreamIFSlave(sim=self.sim, name=f"{self.name}_rd_in", bitwidth=w,
                                   has_tlast=True)
        self.wr_out = StreamIFMaster(sim=self.sim, name=f"{self.name}_wr_out", bitwidth=w,
                                     has_tlast=True)
        self.s_resp = StreamIFMaster(sim=self.sim, name=f"{self.name}_s_resp", bitwidth=w,
                                     has_tlast=True)
        for ep in (self.s_in, self.buf_w, self.rd_in, self.wr_out, self.s_resp):
            self.add_endpoint(ep)
        #: What this task last heard about the player's position — a LOWER bound, never an upper one.
        self.last_rd = 0
        #: The fill pointer in **sample index**, wrapping at ``2**IDX_BW`` as the RTL's does.
        self.wr = 0
        #: Commands refused because their slot had already played.  Not a diagnostic: it is the
        #: counted half of the contract, and a run in which it stays zero has not tested it.
        self.n_too_late = 0
        #: Commands that had to wait for the player to make room (the too-far-ahead case).
        self.n_waited = 0
        #: Commands refused because the window was not a whole number of words.
        self.n_misaligned = 0

    @property
    def usable_lead(self) -> int:
        """Samples a command may run ahead of the player — ``depth * spw``.

        The buffer's whole capacity: a slot further ahead than this would wrap onto a sample the
        player has not reached yet.
        """
        return int(self.depth) * int(self.samp_per_word)

    def kernel_task(self) -> KernelTask:
        return KernelTask("rf_samp_buf_loader_task", "rf_samp_buf_loader_task.h",
                          ("buf_w", "rd_in", "s_in", "wr_out", "s_resp"),
                          template_args=(int(self.bitwidth), int(self.samp_per_word),
                                         int(self.depth), int(self.horizon_margin), IDX_BW))

    @sim_only
    def count_too_late(self) -> None:
        """Tally a command whose slot had already played — instrumentation, and marked as such."""
        self.n_too_late += 1

    @sim_only
    def count_waited(self) -> None:
        """Tally a command that had to wait for the player to make room."""
        self.n_waited += 1

    @sim_only
    def count_misaligned(self) -> None:
        """Tally a command whose window was not a whole number of words."""
        self.n_misaligned += 1

    def run_iter(self) -> ProcessGen[None]:
        """The pysim twin.  Data is burst-granular; **rate is word-granular**, and that is the point.

        The predecessor pattern this repo removed in PR #160 relayed a whole burst and charged
        nothing for it, which made the model silently rate-blind.  This body charges
        :attr:`word_cycles` per payload word — the payload loop's *measured* pipeline II — so a host
        that cannot feed the buffer fast enough shows up as the player underrunning rather than as a
        clean run.

        Not a ``get_pipelined`` body, for the reasons recorded in ``plans/pipelined_ops.md``: it
        zero-pads to a length that must be known in advance, and its ``tstart`` assumes an II=1
        fabric-paced producer.
        """
        mask = int(self.depth) - 1
        spw = int(self.samp_per_word)
        wrap = 1 << IDX_BW
        lead_cap = self.usable_lead
        margin = int(self.horizon_margin)

        cmd = yield from self.s_in.get(TxCmd)
        idx = int(cmd.start)
        nsamp = int(cmd.nsamp)
        loaded = 0
        status = RF_SAMP_BUF_OK
        waited = False

        # Payload length is a property of the FRAME: rounded up, and drained in full whatever the
        # verdict, so a refused command cannot desynchronise the ones behind it.
        npay = (nsamp + spw - 1) // spw
        if (idx % spw) or (nsamp % spw):
            status = RF_SAMP_BUF_MISALIGNED
            self.count_misaligned()

        payload = yield from self._drain_payload(npay)
        for x in payload:
            # THE RATE CONTRACT, charged PER WORD and charged HERE rather than after the loop: what
            # matters on this side is *when a slot becomes filled* relative to the player passing it,
            # so a body that stored the whole payload instantly and paid for it afterwards would put
            # the data in the buffer before the hardware could have.  That is the same rate-blindness
            # PR #160 removed from the RX ingress, pointing the other way.
            yield self.timeout(self.word_cycles * self.clk.period)
            if status != RF_SAMP_BUF_OK:
                continue                      # consumed and discarded, never silently stored

            # 1. Wait for room.  Poll first (take the newest position the channel holds), and only
            #    then block -- there is nothing to do until the player advances.
            while True:
                got = yield from self.rd_in.get_nb()
                if got is not None:
                    self.last_rd = int(np.asarray(got).ravel()[-1])
                if sdiff(idx, self.last_rd) < lead_cap:
                    break
                waited = True
                got = yield from self.rd_in.get()
                self.last_rd = int(np.asarray(got).ravel()[-1])

            # 2. Too late: the slot has already played, or is within the staleness margin of it.
            if sdiff(idx, self.last_rd) < margin:
                status = RF_SAMP_BUF_TOO_LATE
                self.count_too_late()
                continue                      # keep draining; the frame is still owed its words

            self.buf_w.mem_write((idx // spw) & mask, int(x))
            loaded += spw
            idx = (idx + spw) % wrap
            self.wr = idx
            yield from self.wr_out.offer(np.array([self.wr], dtype=np.uint64))

        if waited:
            self.count_waited()
        resp = TxResp(tid=int(cmd.tid), status=int(status), nloaded=int(loaded))
        yield from self.s_resp.write(resp)

    def _drain_payload(self, npay: int) -> ProcessGen[list]:
        """Pull exactly *npay* payload words off ``s_in``, however many bursts they arrive in.

        A burst is pysim's quantum and the payload's framing is the *command's*, not the driver's, so
        the two need not line up: a driver may deliver the payload in one burst or several.  This
        keeps reading until the frame is satisfied rather than assuming one burst is one payload —
        which is the assumption that would make the model agree with the hardware only by luck.
        """
        out: list[int] = []
        while len(out) < int(npay):
            words = yield from self.s_in.get()
            out.extend(int(v) for v in np.asarray(words).ravel())
        if len(out) > int(npay):
            raise RuntimeError(
                f"{type(self).__name__} '{self.name}': the payload burst carried {len(out)} words "
                f"for a {int(npay)}-word frame. A burst that straddles two commands would take a "
                f"sample for a tid, so it is refused rather than silently split.")
        return out


@dataclass
class RfSampBufPlayer(FreeRunMod):
    """One word out of the circular buffer, one word to the DAC, every slot, forever.

    **This task may never miss a deadline**, and it is the mirror of
    :class:`~waveflow.hw.rf_samp_buf.RfSampBufIngress`: same shape, opposite failure.  It is
    **free-running**, not demand-driven — it emits on the grid whether or not anybody has asked, and
    whether or not the loader has kept up.

    Its one blocking call is the write to ``s_out``, and that is not waiting for data: it is the
    DAC's own ``TREADY``, which is the metronome this task runs on.  The DAC has a real input FIFO
    and does back-pressure the fabric, so being paced by it is correct.  Being paced by the *loader*
    would not be.

    **Underrun is the counted contract.**  When the play pointer reaches a slot the loader has not
    filled, the slot is emitted anyway — stale from the previous lap, or the buffer's initial
    contents — and :attr:`n_underrun` is incremented.  There are no dropped samples here: a slot is
    either right or stale, and the counter is what tells them apart.
    """

    cpp_kernel_name: ClassVar[str | None] = "rf_samp_buf_player"

    #: **Fabric cycles per firing** — one word out of the buffer, one word to the DAC, per this many
    #: cycles.
    #:
    #: MEASURED, from ``Worst-caseLatency = 2`` in
    #: ``rf_samp_buf_player_task_16_1_2048_16_s_csynth.xml``: a firing costs ``latency + 1`` = **3**
    #: FSM states.  It was **2** until 2026-08-17, justified by symmetry with
    #: :attr:`~waveflow.hw.rf_samp_buf.RfSampBufIngress.fire_cycles` — "the same shape, so the same
    #: cost" — and the report refutes the premise: the ingress reports latency 1, this body 2.  The
    #: extra state is real work: this body reads a BRAM *and* polls the fill channel before it can
    #: write, where the ingress reads a stream and writes a BRAM with nothing to consult.
    #:
    #: **The calibration is anchored, not assumed.**  ``latency + 1`` is what makes the RX ingress's
    #: declared 2 correct at latency 1, and that value is independently corroborated by RTL: at
    #: 256 MSa/s the RX design accepted 58.6% of the stream, which is 0.5/0.853 — exactly the ratio
    #: ``fire_cycles = 2`` predicts.  A guard re-derives all of this from the reports; see
    #: ``tests/examples/test_rf_samp_buf_fire_cycles.py``.
    #:
    #: It is a **rate contract**: the fastest DAC this design can feed is
    #: ``samp_per_word * f_axis / fire_cycles``, and a DAC faster than that underruns with no
    #: protocol event to mark it.  The correction lowered that ceiling by a third — from 150 to
    #: 100 MSa/s at one sample per word on a 300 MHz fabric — and the old value **permitted 50% more
    #: sample rate than the hardware sustains**.  See :meth:`RfSampBufTx.check_rate`.
    fire_cycles: ClassVar[int] = 3

    bitwidth: HwParam[int] = WORD_BW
    samp_per_word: HwParam[int] = 1
    depth: HwParam[int] = BUF_DEPTH
    #: **Words per pysim output burst — a modelling shape, not a hardware parameter.**
    #:
    #: The RTL body writes ONE word per firing and knows nothing about blocks.  This exists because
    #: pysim's quantum on the converter edge is a *block*: ``Rfdc``'s DAC process consumes a whole
    #: ``blksize``-sample burst per event, and a shorter burst is silently zero-padded to it.  So the
    #: twin must hand it one block per write, exactly as the RX ingress *reads* one block per get.
    #:
    #: It does **not** change the rate — :attr:`fire_cycles` is charged per word either way — so the
    #: underrun a slow loader causes is visible whatever this is set to.  1 is the honest default
    #: (one word, one write); a testbench wiring this to a converter sets it to ``blksize // spw``.
    blk_words: int = 1
    #: **Words per second the DAC consumes** — ``samp_rate / samp_per_word`` — or ``None`` to run at
    #: the fabric's rate alone.  A *modelling* input rather than a hardware parameter, and it exists
    #: because pysim cannot deliver the metronome any other way.
    #:
    #: In RTL this task is paced by ``TREADY``: it writes a word, the 2-deep boundary port fills, and
    #: it waits for the DAC to take one.  **pysim has no such back-pressure for a burst write.**
    #: ``StreamIF`` routes intra-burst overflow to an unbounded counter rather than blocking — which
    #: is deliberate (``docs/guide/rf/python/fidelity.md``) but means no queue depth can throttle this
    #: task.  Measured three ways: with the DAC-facing port unbounded, at 2 words deep, and at a whole
    #: block deep, the player emitted a block every 3.4 us at **every** sample rate — the fabric's
    #: rate, never the converter's.
    #:
    #: Left unset the player runs at the FABRIC's rate — which is the loader's rate too, so the
    #: loader could never stay ahead of it and every command would eventually be refused as too late.
    #: That is an artefact of the model, not of the design: at RTL the DAC holds the player to
    #: ``samp_rate`` while the loader keeps the fabric's, which is the headroom the buffer lives in.
    #: Supplying the rate restores it, and the firing then costs ``max(fabric, DAC)``.
    #:
    #: What is still not faithful: pysim charges this per BURST rather than per word, so the player's
    #: position is exact only at block boundaries.  That is why :attr:`n_underrun` is evidence of a
    #: starved loader rather than a measurement of one.
    dac_word_rate: float | None = None
    clk: Clock = field(default_factory=lambda: Clock(freq=300e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        w, d, spw = int(self.bitwidth), int(self.depth), int(self.samp_per_word)
        if d & (d - 1):
            raise ValueError(f"buffer depth must be a power of two (got {d}): the wrap is a mask")
        if int(self.blk_words) < 1:
            raise ValueError(f"blk_words must be at least one word, got {int(self.blk_words)}")
        samp_type(w, spw)
        self.wr_in = StreamIFSlave(sim=self.sim, name=f"{self.name}_wr_in", bitwidth=w,
                                   has_tlast=True)
        self.rd_out = StreamIFMaster(sim=self.sim, name=f"{self.name}_rd_out", bitwidth=w,
                                     has_tlast=True)
        self.buf_r = BramIFMaster(sim=self.sim, name=f"{self.name}_buf_r", bitwidth=w, depth=d,
                                  access="read")
        self.s_out = StreamIFMaster(sim=self.sim, name=f"{self.name}_s_out", bitwidth=w,
                                    has_tlast=True)
        for ep in (self.wr_in, self.rd_out, self.buf_r, self.s_out):
            self.add_endpoint(ep)
        #: The play pointer in **sample index**, wrapping at ``2**IDX_BW`` as the RTL's does.
        self.rd = 0
        #: A LOWER bound on how far the loader has filled.  Lower, because the fill channel drops
        #: rather than stalling the loader — so this counter can only ever *over*-report underrun,
        #: never miss one, which is the honest direction for a fault counter.
        self.last_wr = 0
        #: Slots played that the loader had not filled.  The TX mirror of the RX ingress's dropped
        #: words, and distinct from the ``RFSampIF`` edge's ``underrun``: that one counts whole block
        #: periods the DAC had nothing for, this one counts *buffer slots* that were stale.
        self.n_underrun = 0
        #: Words emitted, in total — the denominator :attr:`n_underrun` is meaningful against.
        self.n_played = 0
        #: Firings so far, and the epoch they are timed from — the absolute grid :meth:`run_iter`
        #: paces on.  See there for why a relative timeout is wrong.
        self._fired = 0
        self._t0 = 0.0

    @property
    def capacity_samp_per_cycle(self) -> float:
        """Samples this player sustains per fabric cycle — ``samp_per_word / fire_cycles``."""
        return int(self.samp_per_word) / float(self.fire_cycles)

    def kernel_task(self) -> KernelTask:
        return KernelTask("rf_samp_buf_player_task", "rf_samp_buf_player_task.h",
                          ("buf_r", "wr_in", "rd_out", "s_out"),
                          template_args=(int(self.bitwidth), int(self.samp_per_word),
                                         int(self.depth), IDX_BW))

    @sim_only
    def count_underrun(self) -> None:
        """Tally a slot played before the loader filled it — instrumentation, and marked as such."""
        self.n_underrun += 1

    @sim_only
    def count_played(self) -> None:
        """Tally a word emitted."""
        self.n_played += 1

    def run_iter(self) -> ProcessGen[None]:
        """The pysim twin: word-granular in **rate and in underrun**, block-shaped on the wire.

        Every slot is visited individually — the buffer is read per word, the play pointer advances
        per word, and underrun is decided per word — because a stale slot is a per-slot fact and
        collapsing it to a burst would be exactly the rate-blindness PR #160 removed from the RX
        side.  What is *shaped* is only the handover: :attr:`blk_words` words are written as one
        burst, because pysim's quantum on the converter edge is a block and ``Rfdc``'s DAC process
        consumes one per event.

        ``fire_cycles`` is charged **per word**, so the rate is the hardware's rather than the
        burst's.  That is what makes a loader which cannot keep up show up here as a nonzero
        :attr:`n_underrun` instead of as a clean run.

        Not a ``get_pipelined`` / ``write_pipelined`` body, for the reasons recorded in
        ``plans/pipelined_ops.md`` and restated on the RX ingress.
        """
        mask = int(self.depth) - 1
        spw = int(self.samp_per_word)
        wrap = 1 << IDX_BW
        nwords = int(self.blk_words)

        out = np.empty(nwords, dtype=np.uint64)
        for k in range(nwords):
            # Poll the fill channel non-blockingly.  Blocking here would be waiting for the loader,
            # which is the one thing this task may not do.
            got = yield from self.wr_in.get_nb()
            if got is not None:
                self.last_wr = int(np.asarray(got).ravel()[-1])

            # `rd - last_wr >= 0` means the play pointer has reached or passed everything known to
            # be filled, so this slot is stale.  It is emitted anyway; that is what a DAC gets.
            if sdiff(self.rd, self.last_wr) >= 0:
                self.count_underrun()

            out[k] = self.buf_r.mem_read((self.rd // spw) & mask)
            self.count_played()
            self.rd = (self.rd + spw) % wrap
            yield from self.rd_out.offer(np.array([self.rd], dtype=np.uint64))

        # Hand off FIRST, then charge.  Charging before the write would make every block arrive one
        # period late -- the player and the converter would serialise rather than overlap.
        yield from self.s_out.write(out)

        # THE RATE CONTRACT: fire_cycles per WORD (what the FABRIC can do) against the DAC's own
        # demand (what it must do), whichever is slower.  In RTL the second term arrives as TREADY;
        # here it has to be supplied, because pysim does not back-pressure a burst write -- see
        # :attr:`dac_word_rate`, where the measurement is recorded.
        #
        # ON AN ABSOLUTE GRID, not a relative timeout, and for exactly the reason
        # ``RFSampIF`` schedules its metronome that way (see
        # ``docs/guide/rf/python/sampling.md#absolute-grid``): a relative wait restarts from wherever
        # ``now`` happens to be when the body finishes, so everything the body yielded for is ADDED to
        # the period and never given back.  Here that is the interface's own transfer time for the
        # burst just written -- 64 words at 250 MHz is a quarter of a block period -- which made the
        # player slip a whole block every fourth firing and the DAC edge underrun periodically.  The
        # error was proportional to the firing index, which is what makes it fatal for a converter
        # rather than merely untidy.  Found by the pattern-B example, whose played stream showed a
        # gap every third block.
        self._fired += 1
        fabric = nwords * self.fire_cycles * self.clk.period
        demand = 0.0 if not self.dac_word_rate else nwords / float(self.dac_word_rate)
        period = max(fabric, demand)
        deadline = self._t0 + self._fired * period
        yield self.timeout(max(0.0, deadline - self.now))


# ---------------------------------------------------------------------------
# The composite: two tasks, two channels, one memory beside the kernel
# ---------------------------------------------------------------------------

@dataclass
class RfSampBufTx(FreeRunMod):
    """The TX sample buffer as one design scope: loader + player + the memory between them.

    ===========================   =============================================================
    ``add_comp(loader/player)``   the two ``hls::task``\\ s inside the generated kernel
    ``add_if(wr_if, rd_if)``      the two progress channels -> ``hls::stream``\\ s **depth 1**
    ``add_rtl_mod(mem)``          the buffer, realized as hand-written Verilog beside the kernel
    ``add_rtl_if(...)``           wrapper wires -> the tasks' memory ports stay boundary ports
    ===========================   =============================================================

    **Both progress channels are depth 1 on purpose**, for the reason the RX one is: each carries a
    running position, only the newest value means anything, and a deeper queue would only serve older
    ones.  Combined with a non-blocking write on one end and a non-blocking poll on the other, "the
    channel is full" simply means "the other side already knows roughly where we are".
    """

    cpp_kernel_name: ClassVar[str | None] = "rf_samp_buf_tx"
    potential_targets: ClassVar[frozenset[str]] = frozenset({COMPOSITE_KERNEL})

    bitwidth: HwParam[int] = WORD_BW
    samp_per_word: HwParam[int] = 1
    depth: HwParam[int] = BUF_DEPTH
    horizon_margin: HwParam[int] = HORIZON_MARGIN
    #: Words per pysim output burst — see :attr:`RfSampBufPlayer.blk_words`.  A modelling shape, not
    #: a hardware parameter, and therefore a plain field rather than an ``HwParam``: it must not
    #: appear in the elaborated signature or the generated top.
    blk_words: int = 1
    #: Words per second the DAC consumes — see :attr:`RfSampBufPlayer.dac_word_rate`.  A modelling
    #: input for the same reason, and a plain field for the same reason.
    dac_word_rate: float | None = None
    clk: Clock = field(default_factory=lambda: Clock(freq=300e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        w, d, spw = int(self.bitwidth), int(self.depth), int(self.samp_per_word)
        self.loader = RfSampBufLoader(sim=self.sim, name=f"{self.name}_loader", bitwidth=w,
                                      samp_per_word=spw, depth=d,
                                      horizon_margin=int(self.horizon_margin), clk=self.clk)
        self.player = RfSampBufPlayer(sim=self.sim, name=f"{self.name}_player", bitwidth=w,
                                      samp_per_word=spw, depth=d, blk_words=int(self.blk_words),
                                      dac_word_rate=self.dac_word_rate, clk=self.clk)
        self.add_comp(self.loader)
        self.add_comp(self.player)

        # loader -> player: how far the buffer is filled.
        wr_if = StreamIF(name=f"{self.name}_wr_if", sim=self.sim, clk=self.clk, bitwidth=w, depth=1)
        wr_if.bind(ep_name="master", endpoint=self.loader.wr_out)
        wr_if.bind(ep_name="slave", endpoint=self.player.wr_in)
        self.add_if(wr_if)
        # player -> loader: how far the buffer has played.
        rd_if = StreamIF(name=f"{self.name}_rd_if", sim=self.sim, clk=self.clk, bitwidth=w, depth=1)
        rd_if.bind(ep_name="master", endpoint=self.player.rd_out)
        rd_if.bind(ep_name="slave", endpoint=self.loader.rd_in)
        self.add_if(rd_if)

        # `mem`, not `buf`: the attribute name becomes the Verilog instance name and `buf` is a
        # primitive gate (the wrapper emitter refuses it by name).
        self.mem = T2pBram(sim=self.sim, name=f"{self.name}_mem", dwidth=w, depth=d)
        self.add_rtl_mod(self.mem)
        w_if = BramIF(name=f"{self.name}_bufw_if", sim=self.sim)
        w_if.bind(ep_name="master", endpoint=self.loader.buf_w)
        w_if.bind(ep_name="slave", endpoint=self.mem.wr_port)
        self.add_rtl_if(w_if)
        r_if = BramIF(name=f"{self.name}_bufr_if", sim=self.sim)
        r_if.bind(ep_name="master", endpoint=self.player.buf_r)
        r_if.bind(ep_name="slave", endpoint=self.mem.rd_port)
        self.add_rtl_if(r_if)

        #: ``add_comp`` x ``add_endpoint`` order, with the two progress channels' endpoints removed.
        #: The two ``buf_*`` entries are ports of the KERNEL, joined to the memory in the wrapper.
        self.boundary = ["s_in", "buf_w", "s_resp", "buf_r", "s_out"]

        # Convenience refs for the testbenches — the boundary endpoints live on the children.
        self.s_in = self.loader.s_in
        self.s_resp = self.loader.s_resp
        self.s_out = self.player.s_out

    @property
    def nsamp_held(self) -> int:
        """Samples the buffer holds — ``depth`` words of ``samp_per_word`` samples each."""
        return int(self.depth) * int(self.samp_per_word)

    @property
    def capacity_samp_per_cycle(self) -> float:
        """Samples this buffer sustains per fabric cycle — the player's rate contract."""
        return self.player.capacity_samp_per_cycle

    def max_samp_rate(self, f_axis: float, samp_per_word: int | None = None) -> float:
        """The fastest DAC this buffer can feed, in samples/s, at *f_axis*.

        ``samp_per_word * f_axis / fire_cycles`` — the same *arithmetic* as the RX side's, but not the
        same number: the player's ``fire_cycles`` is 3 against the ingress's 2, measured rather than
        assumed to match.  **Not** the port's capacity either, which is ``samp_per_word * f_axis``;
        the difference between those two is where a design silently underruns.
        """
        spw = int(self.samp_per_word) if samp_per_word is None else int(samp_per_word)
        return float(f_axis) * spw / RfSampBufPlayer.fire_cycles

    def check_rate(self, samp_rate: float, f_axis: float, samp_per_word: int | None = None) -> float:
        """Refuse a DAC this buffer cannot feed, and return the utilisation.

        The mirror of :meth:`~waveflow.hw.rf_samp_buf.RfSampBufRx.check_rate`, and it fails the other
        way: too fast a converter costs the RX side dropped samples and costs the TX side
        **underruns** — a DAC that plays whatever is in its FIFO when the period comes due, including
        the last word again.

        Owned here rather than by a testbench because a module's throughput is part of its interface
        contract — but it needs the *pairing*, so the converter's rate is an argument.
        """
        spw = int(self.samp_per_word) if samp_per_word is None else int(samp_per_word)
        cap = self.max_samp_rate(f_axis, spw)
        if float(samp_rate) > cap:
            raise ValueError(
                f"{type(self).__name__} '{self.name}': {float(samp_rate):g} samples/s exceeds what "
                f"the player can sustain — samp_per_word * f_axis / fire_cycles = "
                f"{spw} * {float(f_axis):g} / {RfSampBufPlayer.fire_cycles} = {cap:g}. A DAC cannot "
                f"be told to wait, so the shortfall is not delayed, it is PLAYED AS STALE DATA. "
                f"pysim WILL show it as a nonzero `n_underrun` on the player. Lower the rate, widen "
                f"the word (samp_per_word), or make the player body cheaper.")
        return float(samp_rate) / cap

    @property
    def n_underrun(self) -> int:
        """Slots played before the loader filled them."""
        return int(self.player.n_underrun)

    @property
    def n_played(self) -> int:
        """Words the player emitted in total."""
        return int(self.player.n_played)

    @property
    def n_too_late(self) -> int:
        """Commands refused because their slot had already played."""
        return int(self.loader.n_too_late)

    @property
    def n_waited(self) -> int:
        """Commands that had to wait for the player to make room."""
        return int(self.loader.n_waited)

    @property
    def n_misaligned(self) -> int:
        """Commands refused because their window was not a whole number of words."""
        return int(self.loader.n_misaligned)
