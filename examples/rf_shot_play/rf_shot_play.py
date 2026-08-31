"""rf_shot_play.py — Stage B of ``plans/rf_shot_buf.md``: load a waveform once, play it ``nrepeat``
times, out of a real converter.

The **modules** are framework (:mod:`waveflow.hw.rf_shot_tx`, :mod:`waveflow.hw.rf_shot_buf`,
:mod:`waveflow.hw.rf_relayout`); what is here is what an example should be — a graph that wires them
to a converter, a scenario that reaches every verdict the design claims, and a golden::

    StreamDriver --[ShotTxHdr | dense words ... TLAST]--> RfShotTx.s_in
    RfShotTx.resp_out --> StreamSink                 (one ShotTxResp per header)
    RfShotTx.samp_out --> Rfdc.tx_streams[0] | Rfdc.tx_rf --RFSampIF--> RfDataSink

**The same user story as** ``examples/rf_repeat_play``, **deliberately.**  That one is Stage 1 of
``plans/rf_samp_new.md`` and does it with the acked-stream transmitter: a slot grid, a pending FIFO,
a status channel, a lateness verdict and a scheduler that has to learn where "now" is by asking.
This one has none of those, and the point of building it twice is that
``docs/guide/rf/choosing.md``'s comparison becomes **checkable** rather than asserted — two designs,
one question, and the difference visible in the diagrams rather than in prose.

**The converter is really here**, not a stand-in sink, because the one thing a playout design exists
to satisfy is that a DAC cannot be told to wait.  The tile is DAC-only (``n_rx=0``): wiring a fake
ADC in would add a metronome nothing drains.

Two scenarios, and why they cannot be one
-----------------------------------------
:data:`GATE_FRAMES` reaches four of the five verdicts, and :data:`SHORT_FRAMES` reaches the fifth.
That split is forced by the design rather than chosen.  Once a shot is accepted the buffer is
**busy** until its play-set finishes, and a file-driven driver pushes every frame back to back — so
**at most one load per scenario can succeed**, and every later one is :data:`~waveflow.hw.rf_shot_tx.SHOT_BUSY`
by construction.  That is not a limitation of the testbench: it is what :data:`SHOT_BUSY` *is*, and a
host that wanted two loads would read its verdicts and wait, which a vector file cannot do.

So the successful load is the first frame of each scenario, and each scenario asks a different
question of it: :data:`GATE_FRAMES` asks *does a whole shot play, three times, bit-exact*, and
:data:`SHORT_FRAMES` asks *does a truncated transfer produce a verdict instead of a hang.*

What the short scenario proves that no counter can
--------------------------------------------------
``TLAST`` before the shot is full is the failure this design's response exists for.  A DMA reports
success either way — ``sendchannel.transfer()`` knows it pushed bytes — so from the host side a
half-loaded buffer is indistinguishable from a full one.  The verdict says which, and
``nsamp_loaded`` says how much: the difference between it and the header's ``nsamp`` *is* the
diagnosis.  And nothing plays, because a shot that is not playable is handed a repeat count of zero.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

import numpy as np

HERE = Path(__file__).resolve().parent

from waveflow.build.composite_gen import RFSOC4X2_CLK_HZ  # noqa: E402
from waveflow.hw.clock import Clock  # noqa: E402
from waveflow.hw.codegen_targets import SEQUENTIAL_XSI_TB  # noqa: E402
from waveflow.hw.hw_freerun import FreeRunMod  # noqa: E402
from waveflow.hw.interface import StreamIF  # noqa: E402
from waveflow.hw.rf_relayout import to_dense, to_slots  # noqa: E402
from waveflow.hw.rf_sample_if import RFSampIF  # noqa: E402
from waveflow.hw.rf_shot_tx import (  # noqa: E402
    SHOT_BUSY,
    SHOT_END,
    SHOT_LOAD,
    SHOT_LOADED,
    SHOT_SHORT,
    SHOT_STATUS_NAMES,
    SHOT_TX_SCHEMA_CLASSES,
    SHOT_WRONG_LEN,
    SHOT_ZERO_LEN,
    RfShotTx,
    ShotTxHdr,
    ShotTxResp,
)
from waveflow.hw.rfdc_samp_word import RfdcSampWord, Rfsoc4x2SampWord, pack  # noqa: E402
from waveflow.simulation.rf_tb import RfDataSink  # noqa: E402
from waveflow.simulation.simulation import Simulation  # noqa: E402
from waveflow.simulation.stream_tb import StreamDriver, StreamSink  # noqa: E402

from examples.rf_loopback.rfdc import Rfdc  # noqa: E402

__all__ = [
    "BLKSIZE", "CODE_BASE", "DEPTH", "GATE_FRAMES", "NREPEAT", "NWORD", "N_BLK", "SAMP_RATE",
    "SHORT_FRAMES", "SHORT_WORDS", "STARTUP_BLOCKS", "WORD", "XSI_N_CYCLES",
    "RfShotPlayTB", "check_played", "check_responses", "expected_responses", "frame",
    "blocks_to_codes", "expected_plays", "first_play_offset", "played_samples", "responses",
    "run_pysim", "shot_codes", "shot_dense", "shot_slots", "write_scenario",
]

# ---------------------------------------------------------------------------
# The geometry — every number here is stated rather than defaulted
# ---------------------------------------------------------------------------

#: **The gated packing convention**: the RFSoC 4x2's, four 14-in-16 samples in a 64-bit beat.  Four
#: rather than one, because :attr:`~waveflow.hw.rfdc_samp_word.RfdcSampWord.justify_shift` is then
#: non-zero and the re-layout stage is a real conversion rather than the identity — the caveat
#: ``plans/rf_shot_buf.md`` § *The caveat* is about, and a gate asserts it below.
WORD: type[RfdcSampWord] = Rfsoc4x2SampWord.specialize(samp_per_word=4)

#: Words in one shot, and words the memory holds.  A shot SHORTER than the memory on purpose: a shot
#: that exactly filled the buffer would make an off-by-one in the address arithmetic invisible,
#: because every address would be in range either way.
NWORD = 64
DEPTH = 256

#: Plays of the loaded shot.  Three, not one: a repeat player that plays once is a player with the
#: repeat untested, and the boundary between plays is where a token-driven design goes wrong.
NREPEAT = 3

#: Samples per converter block — pysim's quantum on the RF edge, and the unit
#: :meth:`~waveflow.hw.rf_sample_if.RFSampIF.assert_clean` counts in.  One shot is exactly four
#: blocks, so a play boundary lands on a block boundary and a slipped play is visible as a block
#: rather than as a fraction of one.
BLKSIZE = 64

#: Sample rate.  **0.256 words per fabric cycle** — ``256e6 / (4 * 250e6)`` — which is deliberately
#: the same fabric load ``examples/rf_samp_buf_tx`` runs at, so the two are comparable at the level
#: that matters (how hard the DAC leans on the design).  Four samples per beat is what buys the 4x
#: sample rate for the same word rate.
#:
#: **Not the design's ceiling, and the difference is worth knowing.**  The inner loops run at II=1,
#: so the chain can hand over one word per cycle — but between plays the reader task re-fires, and a
#: task boundary costs 3 cycles (``plans/witness/task_loop``).  A converter asking for exactly one
#: word per cycle would therefore underrun at every play boundary however fast the loops are.  The
#: sustainable rate is set by that gap, not by the II.
SAMP_RATE = 256e6

#: The first converter code of the ramp.
CODE_BASE = 1000

#: Blocks the DAC plays before the first loaded sample can reach it — the **declared** startup
#: transient, and :meth:`~waveflow.hw.rf_sample_if.RFSampIF.assert_clean` checks it exactly rather
#: than tolerating it.  It is the pipeline's block latency: the loader must read a header and hand
#: over ``NWORD`` words, the buffer must fill, the token must cross, and only then does the reader
#: start.  **Measured, then pinned** — a change in it is a finding needing an explanation, not a
#: constant to re-tune.
STARTUP_BLOCKS = 2

#: Block periods the converter's metronome runs: the transient plus exactly the playout, and **no
#: tail**.  A trailing block with nothing scheduled is an underrun like any other, and it would put
#: :attr:`~waveflow.hw.rf_sample_if.RFSampIF.last_underrun_idx` past the transient — turning "the run
#: ended" into what reads as a steady-state fault.
N_BLK = STARTUP_BLOCKS + NREPEAT * (NWORD * int(WORD.samp_per_word)) // BLKSIZE

#: Words the short scenario actually sends — half a shot, so ``nsamp_loaded`` is unambiguous (a
#: quarter or an eighth would also work; what must not happen is a number that could be confused with
#: the full length or with zero).
SHORT_WORDS = NWORD // 2

#: Fixed run bound for the generated XSI main — a **testbench constant, not a latency**.  The sinks
#: timestamp the real completion and the recorded cycle counts are those measurements.
#:
#: Chosen so the RTL converter runs the SAME number of block periods pysim's metronome does.  The XSI
#: DAC model has no ``n_blk``: it plays on its grid for as long as the loop runs, and every block past
#: the playout is a zero-fill exactly as a trailing block is in pysim.  At
#: ``words_per_cycle * samp_per_word / blksize`` blocks per cycle — 0.016 here — :data:`N_BLK` blocks
#: is 875 cycles, and 900 is that with a little room for the sink to write the last one.
XSI_N_CYCLES = 900


# ---------------------------------------------------------------------------
# The waveform
# ---------------------------------------------------------------------------

def shot_codes(nword: int = NWORD, base: int = CODE_BASE) -> np.ndarray:
    """The ramp, in **converter codes** — ``base + i`` at sample ``i``, wrapped into 14 bits signed.

    A ramp rather than a constant because the failures a shot player has are *plausible*: a play
    offset by a word, a play that stopped part way, the second half of the previous shot.  Every one
    of those survives a constant check without a murmur.
    """
    n = int(nword) * int(WORD.samp_per_word)
    lo = 1 << (int(WORD.bits_per_samp) - 1)
    return ((np.arange(n, dtype=np.int64) + int(base) + lo) % (1 << int(WORD.bits_per_samp))) - lo


def shot_slots(nword: int = NWORD, base: int = CODE_BASE) -> np.ndarray:
    """The ramp as **converter** words — what the DAC ultimately plays.

    Through :func:`~waveflow.hw.rfdc_samp_word.pack`, never a hand-rolled shift: the slot order and
    the justification are the word type's to decide, and a second statement of them here is the bug
    that hides at one sample per word.
    """
    return np.asarray(pack(WORD, shot_codes(nword, base).reshape(1, -1)), dtype=np.uint64).ravel()


def shot_dense(nword: int = NWORD, base: int = CODE_BASE) -> np.ndarray:
    """The ramp as **densely-packed** words — what a HOST sends.

    This is the whole of what the logic-side port buys: a host writes samples at the converter's
    *resolution*, four 14-bit values at 14-bit stride in a 64-bit beat, and knows nothing about
    justification or 14-in-16.  :class:`~waveflow.hw.rf_relayout.RfRelayoutToSlots` inside the design
    turns them into slots on the way out, so the converter's packing can change without this function
    changing.
    """
    return to_dense(WORD, shot_slots(nword, base))


# ---------------------------------------------------------------------------
# The scenarios
# ---------------------------------------------------------------------------

def frame(opcode: int, tid: int, nsamp: int, nrepeat: int, payload: np.ndarray) -> np.ndarray:
    """One AXI-Stream **frame**: the header, then the payload, ``TLAST`` on the last word.

    A burst in the bundle *is* a frame — the pysim :class:`~waveflow.simulation.stream_tb.StreamDriver`
    writes one burst per ``write`` and the XSI ``AxisMaster`` raises ``TLAST`` on each burst's last
    beat — so the two backends carry the same boundary rather than two encodings of it.
    """
    h = ShotTxHdr()
    h.opcode, h.tid, h.nsamp, h.nrepeat = int(opcode), int(tid), int(nsamp), int(nrepeat)
    words = np.asarray(h.serialize(int(WORD.bitwidth)), dtype=np.uint64).ravel()
    return np.concatenate([words, np.asarray(payload, dtype=np.uint64).ravel()])


def _nsamp() -> int:
    return NWORD * int(WORD.samp_per_word)


def gate_frames() -> list[np.ndarray]:
    """Four of the five verdicts, in one stream, in an order whose outcome is not a race.

    ``tid`` 1 is the only load that can succeed — everything after it arrives while the shot is
    playing — so the three that follow exercise the refusals against a *busy* buffer, and the
    malformed-before-transient rule is what makes their verdicts distinguishable:

    ==========  =========================================  ==========================
    ``tid`` 1   a whole shot, three plays                   :data:`SHOT_LOADED`
    ``tid`` 2   a whole shot, arriving mid-play             :data:`SHOT_BUSY`
    ``tid`` 3   ``nsamp`` the buffer was not built for      :data:`SHOT_WRONG_LEN`
    ``tid`` 4   ``nsamp == 0``, and no payload at all       :data:`SHOT_ZERO_LEN`
    ``tid`` 5   ``SHOT_END`` — the fence                    :data:`SHOT_LOADED`
    ==========  =========================================  ==========================

    ``tid`` 3 is the one that would be a race if the order were different: it is malformed **and**
    badly timed, and the design promises to report the fault the host can fix.  A build that
    reordered the two tests would return ``SHOT_BUSY`` here and this scenario would say so.

    ``tid`` 4 carries no payload, so its ``TLAST`` lands on the header beat itself — the empty-frame
    path, which is a distinct branch in both twins and would otherwise never be exercised.
    """
    dense = shot_dense()
    empty = np.zeros(0, dtype=np.uint64)
    return [
        frame(SHOT_LOAD, 1, _nsamp(), NREPEAT, dense),
        frame(SHOT_LOAD, 2, _nsamp(), 1, dense),
        frame(SHOT_LOAD, 3, _nsamp() + int(WORD.samp_per_word), 1, dense),
        frame(SHOT_LOAD, 4, 0, 1, empty),
        frame(SHOT_END, 5, 0, 0, empty),
    ]


def short_frames() -> list[np.ndarray]:
    """The fifth verdict, and the reason the response exists.

    One frame, declaring a whole shot and carrying half of one.  A DMA transfer of exactly these
    bytes completes cleanly, so this is the case a host cannot see.  The ``SHOT_END`` behind it is a
    fence: its response proves the loader processed the short frame rather than stalling on the words
    that never came, which is the difference between a verdict and a hang.
    """
    return [
        frame(SHOT_LOAD, 9, _nsamp(), 1, shot_dense()[:SHORT_WORDS]),
        frame(SHOT_END, 10, 0, 0, np.zeros(0, dtype=np.uint64)),
    ]


#: The two scenarios, materialized once so both backends and the golden read the same list.
GATE_FRAMES = gate_frames()
SHORT_FRAMES = short_frames()


def expected_responses(frames=None) -> list[tuple[int, int, int]]:
    """``(tid, status, nsamp_loaded)`` the design must produce, derived from the frames themselves.

    Derived rather than transcribed: a scenario edited without its golden is how a gate comes to
    assert what the design happens to do.  The rules here are the module's, restated in the smallest
    form that can be read at a glance — and if they ever disagree with
    :class:`~waveflow.hw.rf_shot_tx.ShotTxLoad`, the disagreement is the finding.
    """
    frames = GATE_FRAMES if frames is None else frames
    w, spw, full = int(WORD.bitwidth), int(WORD.samp_per_word), _nsamp()
    hn = ShotTxHdr.nwords_per_inst(w)
    out: list[tuple[int, int, int]] = []
    busy = False
    for f in frames:
        h = ShotTxHdr().deserialize(np.asarray(f, dtype=np.uint64)[:hn], word_bw=w)
        took = min(int(np.asarray(f).size) - hn, NWORD)
        if int(h.opcode) == SHOT_END:
            out.append((int(h.tid), SHOT_LOADED, 0))
        elif int(h.nsamp) == 0:
            out.append((int(h.tid), SHOT_ZERO_LEN, 0))
        elif int(h.nsamp) != full:
            out.append((int(h.tid), SHOT_WRONG_LEN, 0))
        elif busy:
            out.append((int(h.tid), SHOT_BUSY, 0))
        else:
            out.append((int(h.tid), SHOT_LOADED if took == NWORD else SHOT_SHORT, took * spw))
            busy = True
    return out


def expected_plays(frames=None) -> int:
    """Plays the accepted load in *frames* is worth — ``nrepeat`` for a whole shot, zero for a short
    one, and zero if nothing was accepted at all."""
    frames = GATE_FRAMES if frames is None else frames
    w = int(WORD.bitwidth)
    hn = ShotTxHdr.nwords_per_inst(w)
    for f, (_tid, status, _n) in zip(frames, expected_responses(frames)):
        if status != SHOT_LOADED:
            continue
        h = ShotTxHdr().deserialize(np.asarray(f, dtype=np.uint64)[:hn], word_bw=w)
        if int(h.opcode) != SHOT_END:
            return int(h.nrepeat)
    return 0


def write_scenario(root, frames=None, name: str = "cmd") -> None:
    """Materialize ``<root>/vectors/<name>`` — the frames BOTH backends drive in.

    One writer, so the RTL run and the pysim golden cannot start from different bytes; and one burst
    per frame, so ``TLAST`` lands where the header said the payload ends.
    """
    from waveflow.utils.burst_io import write_burst_bundle

    write_burst_bundle(list(GATE_FRAMES if frames is None else frames),
                       Path(root) / "vectors" / name)


# ---------------------------------------------------------------------------
# The graph
# ---------------------------------------------------------------------------

@dataclass
class RfShotPlayTB(FreeRunMod):
    """A driver pushing frames, the shot transmitter, a real DAC, and two sinks.

    **There is no feedback path anywhere in this diagram** except the design's own ``done`` token,
    and that absence is the lesson ``plans/rf_shot_buf.md`` § *Stage D* wants to teach: load, play,
    compare.  Contrast :class:`~examples.rf_repeat_play.rf_repeat_play.RfRepeatPlayTB`, whose host
    cannot be a file-driven driver at all because its schedule depends on the DUT's own responses.
    Here the testbench does one thing: push frames.
    """

    potential_targets: ClassVar[frozenset[str]] = frozenset({SEQUENTIAL_XSI_TB})

    nword: int = NWORD
    depth: int = DEPTH
    blksize: int = BLKSIZE
    n_blk: int = N_BLK
    samp_rate: float = SAMP_RATE
    axis_freq: float = RFSOC4X2_CLK_HZ
    #: The converter's packing convention, as one type.  Everything downstream — the DUT's width, the
    #: re-layout's shift, the block-to-word arithmetic — is read off it, never restated.
    word: type[RfdcSampWord] = WORD
    #: Which scenario the pysim run and the generated XSI main drive.  ``"cmd"`` is
    #: :data:`GATE_FRAMES`; the short-load run uses its own bundle and its own main.
    in_bundle: str = "vectors/cmd"
    #: Fixed run bound for the generated XSI main — a testbench constant, not a latency.
    n_cycles: int = XSI_N_CYCLES
    axis_clk: Clock = field(default_factory=lambda: Clock(freq=RFSOC4X2_CLK_HZ))

    def __post_init__(self) -> None:
        super().__post_init__()
        self.axis_clk = Clock(name=f"{self.name}_axis_clk", freq=float(self.axis_freq))
        self.samp_clk = Clock(name=f"{self.name}_samp_clk", freq=float(self.samp_rate))

        self.rfdc = Rfdc(name=f"{self.name}_rfdc", sim=self.sim, n_rx=0, n_tx=1, word=self.word)
        w = self.rfdc.axis_bitwidth
        self.dut = RfShotTx.for_word(
            self.word, depth=int(self.depth), nword=int(self.nword), sim=self.sim,
            name=f"{self.name}_dut", clk=self.axis_clk,
            # pysim's quantum on the converter edge is a BLOCK: the Rfdc's DAC process takes one
            # blksize burst per event and refuses a partial one.  A modelling shape only.
            blk_words=int(self.blksize) // int(self.word.samp_per_word),
            # The metronome, handed over directly: pysim does not back-pressure a burst write, so
            # this is the only way the converter's rate reaches the player.  See
            # ShotTxPlay.dac_word_rate.
            dac_word_rate=float(self.samp_rate) / int(self.word.samp_per_word))
        self.drv = StreamDriver(sim=self.sim, name=f"{self.name}_drv", bitwidth=w,
                                in_bundle=str(self.in_bundle), has_tlast=True)
        self.resp_snk = StreamSink(sim=self.sim, name=f"{self.name}_resp_snk", bitwidth=w,
                                   out_bundle="vectors/resp", has_tlast=True)
        self.sink = RfDataSink(name=f"{self.name}_sink", sim=self.sim, out_bundle="vectors/rf_out")
        for c in (self.dut, self.rfdc, self.drv, self.resp_snk, self.sink):
            self.add_comp(c)

        # --- the RF domain: one interface, one metronome (there is no ADC) ----------------------
        self.dac_if = RFSampIF(name=f"{self.name}_dac_if", sim=self.sim, samp_clk=self.samp_clk,
                               n_ch=1, blksize=int(self.blksize), n_blk=int(self.n_blk))
        self.dac_if.bind("tx", self.rfdc.tx_rf)
        self.dac_if.bind("rx", self.sink.rf_ep)
        self.add_if(self.dac_if)

        # --- the PL domain ----------------------------------------------------------------------
        # No depth overrides on the three that become the DUT's own boundary ports: a top-level AXIS
        # argument cannot carry a FIFO depth (Vitis ignores the pragma, sometimes silently — see
        # reference-fifo-depth-is-physical).  What elasticity this design needs is inside it, and it
        # is the memory.
        for nm, master, slave in (("cmd", self.drv.stream_ep, self.dut.s_in),
                                  ("resp", self.dut.resp_out, self.resp_snk.stream_ep),
                                  ("dac", self.dut.samp_out, self.rfdc.tx_streams[0])):
            ifc = StreamIF(name=f"{self.name}_{nm}_axis", sim=self.sim, clk=self.axis_clk,
                           bitwidth=w)
            ifc.bind("master", master)
            ifc.bind("slave", slave)
            self.add_if(ifc)
            setattr(self, f"{nm}_axis", ifc)

    @property
    def blk_period(self) -> float:
        """Seconds per converter block — the metronome's own period."""
        return int(self.blksize) / float(self.samp_rate)

    @property
    def run_until(self) -> float:
        """Simulated horizon: the metronome's own length plus a two-block tail.

        A testbench constant, not a latency.  The converter is a free-running event source that never
        exhausts — that is what a DAC does — so ``env.run()`` with no bound would never return; the
        run is bounded the way the converter is, with a small margin so the last verdict has
        somewhere to land.
        """
        return (int(self.n_blk) + 2) * self.blk_period

    @property
    def words_per_cycle(self) -> float:
        """How hard the DAC leans on the fabric — ``samp_rate / (samp_per_word * f_axis)``.

        Derived, never declared: the same quantity the XSI converter model is constructed with, so a
        rate changed here cannot leave the two backends running at different speeds.
        """
        return float(self.samp_rate) / (int(self.word.samp_per_word) * float(self.axis_freq))


# ---------------------------------------------------------------------------
# Running it, and reading what came out
# ---------------------------------------------------------------------------

def run_pysim(root=None, frames=None, **kw) -> RfShotPlayTB:
    """Build the graph, run it to the metronome's horizon, return the testbench.

    The lifecycle is spelled out rather than delegated to
    :meth:`~waveflow.simulation.simulation.Simulation.run_sim`, which takes no bound — see
    :attr:`RfShotPlayTB.run_until`.
    """
    import tempfile

    tb = RfShotPlayTB(name="tb", sim=Simulation(), **kw)
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(root or tmp)
        write_scenario(base, frames, name=Path(tb.in_bundle).name)
        tb.drv.root = base
        tb.resp_snk.root = base
        tb.sink.root = base
        sim = tb.sim
        for obj in sim._sim_objs:
            obj.pre_sim()
        for obj in sim._sim_objs:
            proc = obj.run_proc()
            if proc is not None:
                sim.env.process(proc)
        try:
            sim.env.run(until=tb.run_until)
        except Exception:
            for obj in sim._sim_objs:
                obj.error_cleanup()
            raise
        for obj in sim._sim_objs:
            obj.post_sim()
    return tb


def responses(tb: RfShotPlayTB) -> list[tuple[int, int, int]]:
    """``(tid, status, nsamp_loaded)`` in arrival order, read off the response **stream**.

    Off the wire rather than off the module's own list, because the wire is what a host sees and it
    is the half a counter cannot vouch for: a design that decided correctly and serialized wrongly
    passes every internal check.
    """
    if not tb.resp_snk.words:
        return []
    words = np.concatenate(tb.resp_snk.words).ravel()
    n = ShotTxResp.nwords_per_inst(int(WORD.bitwidth))
    out = []
    for i in range(0, words.size - n + 1, n):
        r = ShotTxResp().deserialize(words[i:i + n], word_bw=int(WORD.bitwidth))
        out.append((int(r.tid), int(r.status), int(r.nsamp_loaded)))
    return out


def blocks_to_codes(blocks) -> np.ndarray:
    """RF blocks (normalized reals) -> **converter codes**, signed.

    In codes rather than slot values, and that is the whole point of the buffer's logic-side port:
    codes are what a host wrote and what :func:`shot_codes` states, so the comparison is against the
    thing the user asked for rather than against a re-derivation of the bus layout.  The scale is the
    converter's own — ``Rfdc`` dequantizes through ``SampType`` at ``bits_per_samp``, so
    ``code = round(x * 2**(bits_per_samp-1))`` is that step run backwards, and inverting it here
    rather than at ``bits_per_samp_pack`` is what keeps the justification out of the golden.
    """
    if not len(blocks):
        return np.zeros(0, dtype=np.int64)
    flat = np.concatenate([np.asarray(b, dtype=np.float64).ravel() for b in blocks])
    return np.rint(flat * float(1 << (int(WORD.bits_per_samp) - 1))).astype(np.int64)


def played_samples(tb: RfShotPlayTB) -> np.ndarray:
    """What the DAC actually played, block by block, as signed converter codes.

    Read off the **RF sink** — the far side of the converter — so the comparison covers the
    re-layout, the playout and the converter's own unpack rather than only the design's output port.
    Block ``j`` covers edge slots ``[j*blksize, (j+1)*blksize)``, underruns included: the edge
    zero-fills a block it had nothing for, so a gap is *present* in the array rather than absent from
    it, and the grid stays readable straight off the index.
    """
    return blocks_to_codes(tb.sink.blocks)


def first_play_offset(played: np.ndarray) -> int:
    """Index in *played* where the waveform first appears — the origin of the played grid.

    Neither backend may assume played sample *i* is grid slot *i*: the converter's grid and the
    design's first word start at different instants, and the offset between them is a property of the
    startup transient.  What is fixed is that it is a **constant**, so it is measured once here and
    every later position is absolute against it.
    """
    want = shot_codes()
    n = min(16, int(want.size))
    for i in range(max(0, int(played.size) - n + 1)):
        if np.array_equal(played[i:i + n], want[:n]):
            return i
    return -1


# ---------------------------------------------------------------------------
# The acceptance checks — one place, because both backends make the same claim
# ---------------------------------------------------------------------------

def check_responses(got, frames=None, where: str = "") -> None:
    """Every header answered, once, in order, with its own ``tid`` and the right verdict.

    The ordering claim is not decoration: it is the evidence that the **in-band frame stayed
    aligned**.  A refused header that left its payload on the wire would make the next header out of
    payload words, and the first thing that would show is a response carrying somebody else's ``tid``.
    """
    want = expected_responses(frames)
    got = [(int(t), int(s), int(n)) for t, s, n in got]

    def _fmt(rs):
        return [(t, SHOT_STATUS_NAMES.get(s, s), n) for t, s, n in rs]

    if len(got) != len(want):
        raise AssertionError(
            f"{where}{len(got)} responses for {len(want)} headers. Exactly one response per header "
            f"is the contract (plans/rf_shot_buf.md § 'Why no has_response flag'); a missing one is a "
            f"host waiting forever and an extra one is a host correlating the wrong verdict.\n"
            f"  got:  {_fmt(got)}\n  want: {_fmt(want)}")
    for i, (g, wexp) in enumerate(zip(got, want)):
        if g != wexp:
            raise AssertionError(
                f"{where}response {i} is {_fmt([g])[0]}, expected {_fmt([wexp])[0]}.\n"
                f"  got:  {_fmt(got)}\n  want: {_fmt(want)}")


def check_played(played: np.ndarray, n_plays: int = NREPEAT, where: str = "") -> int:
    """The DAC played the loaded ramp, *n_plays* times, contiguously — and returns where it started.

    Three named failure modes, because a bare ``!=`` on hundreds of samples tells a reader nothing:

    * **nothing played** — the shot never reached the converter at all, which is what a refused or
      short load looks like and is a different diagnosis from wrong data;
    * **a truncated play** — the right samples for as far as they go, which is this repo's recurring
      failure and passes any prefix comparison;
    * **a slipped repeat** — a tiling that restarts at the wrong phase, which only a ramp makes
      visible.
    """
    one = shot_codes()
    want = np.tile(one, int(n_plays))
    if int(n_plays) == 0:
        if first_play_offset(played) != -1:
            raise AssertionError(
                f"{where}the waveform reached the converter, and this scenario says it must not. A "
                f"shot that was not accepted as playable must not be played — that is what a repeat "
                f"count of zero is for.")
        return -1
    off = first_play_offset(played)
    if off < 0:
        raise AssertionError(
            f"{where}the loaded ramp never appears in the {played.size} samples the DAC played. "
            f"Either nothing was loaded, or the re-layout is producing words the converter unpacks "
            f"to something else — check the responses first, since a refused load looks exactly like "
            f"this.")
    tail = played[off:off + want.size]
    if tail.size < want.size:
        raise AssertionError(
            f"{where}the playout is {tail.size} samples where {int(n_plays)} plays are "
            f"{want.size}. A play that stopped part way carries the right samples as far as it got, "
            f"so only the count says so.")
    if not np.array_equal(tail, want):
        bad = int(np.argmax(tail != want))
        raise AssertionError(
            f"{where}played sample {bad} (play {bad // one.size}, position {bad % one.size}) is "
            f"{int(tail[bad])}, expected {int(want[bad])} — {int((tail != want).sum())} of "
            f"{want.size} differ. A repeat that restarts at the wrong phase looks exactly like this "
            f"and only a ramp makes it visible.")
    return off
