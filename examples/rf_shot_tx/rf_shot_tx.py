"""rf_shot_tx.py — ``plans/rf_shot_unify.md``: **one transmitter, both play modes**.

The user story two retired examples told *between* them — a finite play-set and an infinite one —
told once by one design::

    StreamDriver --[ShotTxHdr | dense words ... TLAST]--> RfShotTx.s_in
    RfShotTx.resp_out --> StreamSink              (one ShotTxResp per header)
    RfShotTx.samp_out --> Rfdc.tx_streams[0] | Rfdc.tx_rf --RFSampIF--> RfDataSink

**Two scenarios, and they cannot be one.**  A file-driven driver pushes every frame back to back, and
a *finite* shot in flight refuses everything behind it — which is the design working, not a testbench
limitation.  So the finite behaviours and the infinite ones need separate streams, exactly as the
finite predecessor's example needed two for its own reason:

``cmd_finite`` — ``SHOT_LOAD`` with ``nrepeat=3``, then three frames that arrive mid-play

============  ==========================================  ==================
``tid`` 0     a whole shot, three passes                  ``SHOT_LOADED``
``tid`` 1     another load, arriving mid-play             ``SHOT_BUSY``
``tid`` 2     an opcode this design does not know         ``SHOT_BAD_OPCODE``
``tid`` 3     the same, with no payload at all            ``SHOT_BAD_OPCODE``
``tid`` 4     ``SHOT_END`` — the fence                    ``SHOT_LOADED``
============  ==========================================  ==================

``cmd_loop`` — ``SHOT_LOOP``, switched mid-play, then a short one

============  ==========================================  ==================
``tid`` 0     waveform A, played forever                  ``SHOT_LOADED``
``tid`` 1     a bad opcode — **and its payload drains**   ``SHOT_BAD_OPCODE``
``tid`` 2     a bad opcode, no payload                    ``SHOT_BAD_OPCODE``
``tid`` 3     waveform B, **preempting** A                ``SHOT_LOADED``
``tid`` 4     a truncated transfer                        ``SHOT_SHORT``
``tid`` 5     ``SHOT_END``                                ``SHOT_LOADED``
============  ==========================================  ==================

``tid`` 1 and 2 sit between the two loop loads deliberately: their payloads have to be drained, which
buys waveform A airtime on the converter before B arrives.  Without them the switch would happen
before A had played a block.  **Their word counts are what matters** — a header plus a full payload,
then a bare header — so ``plans/rf_shot_geometry.md`` kept both shapes exactly when it changed what
made them refusable.  The frames used to be malformed *lengths*; a length is not something a header
can carry any more, so they are malformed *opcodes* instead, and the airtime is unchanged.

**The converter is really here**, because the one thing a playout design exists to satisfy is that a
DAC cannot be told to wait — and the claim of *both* halves is that neither a handover nor the end of
a finite shot makes it wait: it gets filler, on time, as real beats.

**The shot IS the buffer** (``plans/rf_shot_geometry.md``): the loader writes ``mem[i]``, the player
reads ``mem[i]``, and the only address arithmetic left is the read pointer's wrap at ``depth`` — a
mask, because ``depth`` is a power of two.  There used to be a ``base`` here, placed at the top of
the memory so ``base + offset`` was exercised; the coverage that needed is not lost, the arithmetic
is.  What replaces it is a gate that plays **long enough to wrap**, which is the one piece of
addressing that still exists.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar, NamedTuple

import numpy as np

from waveflow.build.composite_gen import RFSOC4X2_CLK_HZ
from waveflow.hw.clock import Clock
from waveflow.hw.codegen_targets import SEQUENTIAL_XSI_TB
from waveflow.hw.hw_freerun import FreeRunMod
from waveflow.hw.interface import StreamIF
from waveflow.hw.rf_relayout import to_dense
from waveflow.hw.rf_sample_if import RFSampIF
from waveflow.hw.rf_shot_tx import (
    SHOT_BUSY,
    SHOT_END,
    SHOT_LOAD,
    SHOT_LOADED,
    SHOT_LOOP,
    SHOT_BAD_OPCODE,
    SHOT_SHORT,
    SHOT_STATUS_NAMES,
    shot_tx_schemas,
)
from waveflow.hw.rf_shot_tx import FILLER, RfShotTx
from waveflow.hw.rfdc_samp_word import Rfsoc4x2SampWord
from waveflow.simulation.rf_tb import RfDataSink
from waveflow.simulation.simulation import Simulation
from waveflow.simulation.stream_tb import StreamDriver, StreamSink

from examples.rf_loopback.rfdc import Rfdc

HERE = Path(__file__).resolve().parent

#: The converter's word: four 14-in-16 samples in 64 bits.  ``justify_shift() == 2``, so the last
#: stage is a real conversion rather than a pair of wires.
WORD = Rfsoc4x2SampWord.specialize(samp_per_word=4)
WORD_BW = int(WORD.bitwidth)
SPW = int(WORD.samp_per_word)

#: Words the memory holds, which **is** the length of a shot (``plans/rf_shot_geometry.md``).
#:
#: **64, not 256, and that is deliberate.**  It is what ``nword`` was before the shot became the
#: buffer, so the played length is unchanged and every recorded number in this example stays
#: comparable across the change.  A rounder ``depth`` would have made "the numbers held" a
#: coincidence instead of evidence.
DEPTH = 64
#: Samples in a full shot — the largest ``nsamp_loaded`` this design can answer with.
NSAMP = DEPTH * SPW

#: Samples per converter block, and the same number in words.
BLKSIZE = 64
BLK_WORDS = BLKSIZE // SPW

#: The DAC's sample rate against a 250 MHz fabric: 0.256 words per cycle.
SAMP_RATE = 256e6

#: Passes the finite scenario asks for.
NREPEAT = 3

#: Converter blocks the run needs **once the loosely-timed lead is removed** — the horizon this
#: testbench had when the player still carried a metronome, and the part of :data:`N_BLK` that is
#: about the design rather than about the model.
N_BLK_BASE = 20

#: Converter blocks the metronome runs for, and the XSI main's fixed run bound.  A testbench
#: constant, not a latency: the converter never exhausts, so an unbounded run would not return.
#:
#: **27 = N_BLK_BASE + ceil(c_lead / blksize), and the 7 is derived rather than chosen.**  Since
#: ``plans/lt_transient.md`` S2 the player has no metronome, so it runs ahead of the converter's grid
#: by as much as the declared depths along the path allow — :func:`c_lead` samples, 448 at this
#: geometry, which S1 measured at exactly the bound.  That lead is a *prefix* of filler, and at 20
#: blocks the horizon closed with the third pass and the trailing quiet still in the pipe: the
#: playout was truncated by the testbench rather than by the design.  Seven more blocks is precisely
#: the lead, so the run covers what it covered before.
#:
#: **Written down rather than computed here, and gated instead.**  :func:`c_lead` needs a *bound*
#: graph and this is a field default of the testbench that graph comes from, so deriving it at import
#: would be circular.  :func:`check_horizon_covers_the_lead` is the other half: one formula, in
#: :func:`c_lead`, and a check that this number still follows from it.
#:
#: **This is the one number S2 changed**, and everything downstream of it — played sample counts,
#: trailing-filler lengths — moves by construction rather than because the design does anything new.
N_BLK = 27
XSI_N_CYCLES = 1400

#: Base sample codes for the two waveforms.  Far apart and non-overlapping, so "the output switched"
#: is decidable from any single sample.
CODE_A = 1000
CODE_B = 5000
#: Words in the deliberately truncated transfer.
SHORT_WORDS = DEPTH // 2

#: An opcode this design does not know.  ``OPCODE_BW`` is 2 bits and the three legal values are 0, 1
#: and 2, so **3 is the only illegal opcode the wire can carry** — which is what makes
#: :data:`~waveflow.hw.rf_shot_tx.SHOT_BAD_OPCODE` reachable from a real frame rather than only from
#: a hand-built object.
BAD_OPCODE = 3


# ---------------------------------------------------------------------------
# The waveforms
# ---------------------------------------------------------------------------

#: The header/response pair for THIS geometry.  Not the module defaults: since
#: ``plans/rf_shot_wire_format.md`` Part A the response's width is derived from
#: ``depth x samp_per_word``, so a testbench that used the defaults would be a second opinion about
#: the wire — right here only by coincidence, and wrong the moment the geometry changes.
HDR, RESP = shot_tx_schemas(DEPTH, SPW)


def shot_codes(base: int, nwords: int = DEPTH) -> np.ndarray:
    """``nwords * samp_per_word`` distinguishable converter codes, as signed integers.

    ``nwords`` is how much waveform to *build*, which is a testbench's business — not the design's
    retired ``nword`` parameter, which said how long a shot was.  A full one is :data:`DEPTH` words.
    """
    return np.arange(int(base), int(base) + int(nwords) * SPW, dtype=np.int64)


def shot_slots(base: int, nwords: int = DEPTH) -> np.ndarray:
    """The same waveform as **converter words** — what the DAC is handed."""
    from waveflow.hw.rfdc_samp_word import pack

    return np.asarray(pack(WORD, shot_codes(base, nwords).reshape(1, -1)), dtype=np.uint64).ravel()


def shot_dense(base: int, nwords: int = DEPTH) -> np.ndarray:
    """The same waveform as **densely-packed** words — what a host writes.

    Dense on the wire and dense in the memory: the host needs to know nothing about justification,
    and the re-layout at the end of the chain owns the converter's packing.
    """
    return to_dense(WORD, shot_slots(base, nwords))


# ---------------------------------------------------------------------------
# The scenarios
# ---------------------------------------------------------------------------

def frame(opcode: int, tid: int, nrepeat: int, payload: np.ndarray) -> np.ndarray:
    """One AXI-Stream **frame**: the header, then the payload, ``TLAST`` on the last word.

    A burst in the bundle *is* a frame — the pysim ``StreamDriver`` writes one burst per ``write``
    and the XSI ``AxisMaster`` raises ``TLAST`` on each burst's last beat — so the two backends carry
    the same boundary rather than two encodings of it.
    """
    h = HDR()
    h.opcode, h.tid, h.nrepeat = int(opcode), int(tid), int(nrepeat)
    return np.concatenate([np.asarray(h.serialize(word_bw=WORD_BW), dtype=np.uint64).ravel(),
                           np.asarray(payload, dtype=np.uint64).ravel()])


def finite_frames() -> list[np.ndarray]:
    """The finite scenario — see the module docstring."""
    a = shot_dense(CODE_A)
    empty = np.zeros(0, dtype=np.uint64)
    return [
        frame(SHOT_LOAD, 0, NREPEAT, a),
        frame(SHOT_LOAD, 1, 1, a),
        # MALFORMED BEATS TRANSIENT, and that is what tid 2 is for: a finite shot is still playing,
        # so a legal load here would be SHOT_BUSY -- this one is answered SHOT_BAD_OPCODE instead,
        # because a host can fix an opcode and can only retry a busy.
        frame(BAD_OPCODE, 2, 1, a),
        frame(BAD_OPCODE, 3, 1, empty),
        frame(SHOT_END, 4, 0, empty),
    ]


def loop_frames() -> list[np.ndarray]:
    """The infinite scenario — see the module docstring."""
    a, b = shot_dense(CODE_A), shot_dense(CODE_B)
    empty = np.zeros(0, dtype=np.uint64)
    return [
        frame(SHOT_LOOP, 0, 1, a),
        # A FULL PAYLOAD ON A REFUSED FRAME, which is the point: it has to be drained, and draining it
        # is what buys waveform A airtime on the converter before B arrives.
        frame(BAD_OPCODE, 1, 1, a),
        frame(BAD_OPCODE, 2, 1, empty),
        frame(SHOT_LOOP, 3, 1, b),
        frame(SHOT_LOOP, 4, 1, b[:SHORT_WORDS]),
        frame(SHOT_END, 5, 0, empty),
    ]


FINITE_FRAMES = finite_frames()
LOOP_FRAMES = loop_frames()

#: The two scenarios, and the bundle names each reads and writes.
SCENARIOS = (("cmd", FINITE_FRAMES), ("cmd_loop", LOOP_FRAMES))


def expected_responses(frames) -> list[tuple[int, int, int]]:
    """``(tid, status, nsamp_loaded)`` the design must produce, **derived from the frames**.

    Derived rather than transcribed: a scenario edited without its golden is how a gate comes to
    assert what the design happens to do.  The rules are :class:`ShotTxLoader`'s, restated in the
    smallest form that can be read at a glance — and a disagreement is the finding.

    **Four rules where there were six**, and the two that went were the ones that read a length off
    the header.  What is left cannot be read off the header at all except the opcode: whether a
    transfer was short is decided by where ``TLAST`` fell, which is why the ``took`` count below is
    the whole of it.

    ``busy`` is modelled the way the design has it: set by an accepted **finite** load, and cleared
    only when that shot's passes are over.  Within a back-to-back scenario nothing finishes in time,
    so once set it stays set — which is exactly why the two scenarios are two.
    """
    hn = HDR.nwords_per_inst(WORD_BW)
    out: list[tuple[int, int, int]] = []
    busy = False
    for f in frames:
        h = HDR().deserialize(np.asarray(f, dtype=np.uint64)[:hn], word_bw=WORD_BW)
        took = min(int(np.asarray(f).size) - hn, DEPTH)
        op = int(h.opcode)
        if op == SHOT_END:
            out.append((int(h.tid), SHOT_LOADED, 0))
        elif op not in (SHOT_LOAD, SHOT_LOOP):
            out.append((int(h.tid), SHOT_BAD_OPCODE, 0))
        elif busy:
            out.append((int(h.tid), SHOT_BUSY, 0))
        else:
            out.append((int(h.tid), SHOT_LOADED if took == DEPTH else SHOT_SHORT, took * SPW))
            busy = op == SHOT_LOAD
    return out


def write_scenario(root, frames, name: str) -> None:
    """Materialize ``<root>/vectors/<name>`` — the frames BOTH backends drive in.

    One writer, so the RTL run and the pysim golden cannot start from different bytes; and one burst
    per frame, so ``TLAST`` lands where the header said the payload ends.
    """
    from waveflow.utils.burst_io import write_burst_bundle

    write_burst_bundle(list(frames), Path(root) / "vectors" / name)


# ---------------------------------------------------------------------------
# The graph
# ---------------------------------------------------------------------------

@dataclass
class RfShotTxTB(FreeRunMod):
    """A driver pushing frames, the transmitter, a real DAC, and two sinks."""

    potential_targets: ClassVar[frozenset[str]] = frozenset({SEQUENTIAL_XSI_TB})

    depth: int = DEPTH
    blksize: int = BLKSIZE
    n_blk: int = N_BLK
    samp_rate: float = SAMP_RATE
    axis_freq: float = RFSOC4X2_CLK_HZ
    word: type[Rfsoc4x2SampWord] = WORD
    #: Which scenario the pysim run and the generated XSI main drive.  The second scenario reassigns
    #: the bundle names in its own hand-written main.
    in_bundle: str = "vectors/cmd"
    n_cycles: int = XSI_N_CYCLES
    axis_clk: Clock = field(default_factory=lambda: Clock(freq=RFSOC4X2_CLK_HZ))

    def __post_init__(self) -> None:
        super().__post_init__()
        self.axis_clk = Clock(name=f"{self.name}_axis_clk", freq=float(self.axis_freq))
        self.samp_clk = Clock(name=f"{self.name}_samp_clk", freq=float(self.samp_rate))

        self.rfdc = Rfdc(name=f"{self.name}_rfdc", sim=self.sim, n_rx=0, n_tx=1, word=self.word)
        w = self.rfdc.axis_bitwidth
        self.dut = RfShotTx.for_word(
            self.word, depth=int(self.depth), sim=self.sim,
            name=f"{self.name}_dut", clk=self.axis_clk,
            # pysim's quantum on the converter edge is a BLOCK: the Rfdc's DAC process takes one
            # blksize burst per event and refuses a partial one.  A modelling shape only.
            #
            # AND NOTHING ELSE.  There was a `dac_word_rate` here -- the converter's rate, computed
            # by hand as samp_rate / SPW and handed to the player as a metronome -- and
            # `plans/lt_transient.md` S2 retired it.  What paced the player was never hardware: at
            # RTL `TREADY` does it.  The metronome existed to force pysim onto the converter's own
            # time grid so a byte-identical-from-t=0 comparison could pass, and that comparison was
            # a fidelity level loosely-timed modelling does not promise.  The player is now paced by
            # back-pressure alone and runs `c_lead()` samples ahead; the gates align on each
            # backend's own playout log instead.
            blk_words=int(self.blksize) // SPW)
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

        # --- the PL domain -----------------------------------------------------------------------
        # DEPTHS SIZED TO THE BURST, and that is a reversal of what this comment used to say.
        #
        # It used to read "no depth overrides on the three that become the DUT's own boundary ports:
        # a top-level AXIS argument cannot carry a FIFO depth".  The first half is right and the
        # conclusion was backwards.  These three interfaces are owned by the TESTBENCH, not by
        # RfShotTx: codegen elaborates the DUT unbound, so `_check_boundary_depth` never sees them
        # and nothing here reaches the RTL.  What the number does is decide whether pysim's producer
        # stalls against a MODEL -- a StreamDriver is a model of a DMA -- and a whole frame arriving
        # in one event is the honest reading of that.  A depth on an interface the DUT itself owns is
        # still a hardware claim and still refused.  See plans/pysim_burst_backpressure.md S2 Task 0.
        cmd_words = int(self.depth) + 1          # one ShotTxHdr, then the payload
        blk_words = int(self.blksize) // SPW     # what the player hands over at once
        for nm, master, slave, depth in (
                ("cmd", self.drv.stream_ep, self.dut.s_in, 2 * cmd_words),
                ("resp", self.dut.resp_out, self.resp_snk.stream_ep, None),
                ("dac", self.dut.samp_out, self.rfdc.tx_streams[0], 2 * blk_words)):
            ifc = (StreamIF(name=f"{self.name}_{nm}_axis", sim=self.sim, clk=self.axis_clk,
                            bitwidth=w, depth=depth) if depth is not None else
                   StreamIF(name=f"{self.name}_{nm}_axis", sim=self.sim, clk=self.axis_clk,
                            bitwidth=w))
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
        """Simulated horizon: the metronome's own length plus a two-block tail."""
        return (int(self.n_blk) + 2) * self.blk_period

    @property
    def words_per_cycle(self) -> float:
        """How hard the DAC leans on the fabric — the same quantity the XSI converter model is
        constructed with, so a rate changed here cannot leave the two backends at different speeds."""
        return float(self.samp_rate) / (SPW * float(self.axis_freq))


# ---------------------------------------------------------------------------
# Running it, and reading what came out
# ---------------------------------------------------------------------------

def run_pysim(root=None, frames=None, **kw) -> RfShotTxTB:
    """Build the graph, run it to the metronome's horizon, return the testbench."""
    import tempfile

    tb = RfShotTxTB(name="tb", sim=Simulation(), **kw)
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(root or tmp)
        write_scenario(base, FINITE_FRAMES if frames is None else frames,
                       name=Path(tb.in_bundle).name)
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


def responses(tb: RfShotTxTB) -> list[tuple[int, int, int]]:
    """``(tid, status, nsamp_loaded)`` in arrival order, read off the response **stream**.

    Off the wire rather than off the module's own list, because the wire is what a host sees and it
    is the half a counter cannot vouch for: a design that decided correctly and serialized wrongly
    passes every internal check.
    """

    if not tb.resp_snk.words:
        return []
    words = np.concatenate([np.asarray(b).ravel() for b in tb.resp_snk.words])
    n = RESP.nwords_per_inst(WORD_BW)
    out = []
    for i in range(0, words.size - n + 1, n):
        r = RESP().deserialize(words[i:i + n], word_bw=WORD_BW)
        out.append((int(r.tid), int(r.status), int(r.nsamp_loaded)))
    return out


def blocks_to_codes(blocks) -> np.ndarray:
    """``(n_blk, 1, blksize)`` normalized RF blocks -> one flat array of signed converter codes."""
    from waveflow.hw.fixpoint import from_real

    arr = np.asarray(blocks)
    if arr.size == 0:
        return np.zeros(0, dtype=np.int64)
    return np.asarray(from_real(arr.reshape(-1), WORD.samp_type()), dtype=np.int64)


def played_samples(tb: RfShotTxTB) -> np.ndarray:
    """Everything the converter put on the air, as signed codes."""
    return blocks_to_codes(np.asarray(tb.sink.blocks))


def segments(played: np.ndarray) -> list[tuple[bool, np.ndarray]]:
    """The playout split into ``(is_filler, samples)`` runs.

    Filler is a run of :data:`~waveflow.hw.rf_shot_tx.FILLER` codes.  Both waveforms start at
    a non-zero code precisely so this is unambiguous.
    """
    segs: list[tuple[bool, np.ndarray]] = []
    if played.size == 0:
        return segs
    mark = played == FILLER
    start = 0
    for i in range(1, played.size + 1):
        if i == played.size or mark[i] != mark[start]:
            segs.append((bool(mark[start]), played[start:i]))
            start = i
    return segs


# ---------------------------------------------------------------------------
# The LT gate vocabulary — ``plans/lt_transient.md`` S2
# ---------------------------------------------------------------------------
#
# Three gates replace one `array_equal`, and the reason is recorded rather than assumed.  The old
# assertion demanded the two backends be byte-identical **from t=0, including the startup
# transient** -- a fidelity level loosely-timed modelling does not promise, and the only way pysim
# could meet it was a metronome on the player declaring the converter's rate.  That parameter is
# gone (see `RfShotTxTB.__post_init__`), so the two backends now start playing at different
# instants and the comparison has to say what it actually cares about:
#
#   1. `check_phase`               -- each backend against the WAVEFORM, per segment.
#   2. `compare_after_transients`  -- the two backends against each other, aligned on their own logs.
#   3. `transients`                -- the lead, recorded per backend rather than cross-compared.
#
# Gate 1 is what makes relaxing gate 2 safe: an LT transient shifts *when* a segment starts and can
# hide nothing about what is in it, and gate 1 does not care when playing started.


class PlayRun(NamedTuple):
    """One playout run in a captured stream: what was played, where, and for how long.

    ``event`` is the waveform's **base code**, so the log says *what happened* and not merely how
    many times something did.  Two runs that played different waveforms in the same order have
    different logs, and :func:`compare_after_transients` refuses to align them -- which is the point
    of asserting the event sequence before comparing anything.
    """

    event: int
    start: int
    length: int


#: The base codes a playout run may legitimately start on.  A run that starts anywhere else is a
#: phase error, and naming them is what makes :func:`check_phase` a real check rather than a
#: tautology: deriving the base from the run's own first sample would assert nothing about it.
KNOWN_BASES = (CODE_A, CODE_B)

#: Blocks the sink's own queue holds in steady state.  **Zero, and measured** (S1): `RfDataSink`
#: appends and loops with nothing to wait for, so its `DEFAULT_RF_RX_DEPTH` blocks are drained in
#: the same event they are filled.  It is a parameter of :func:`c_lead` rather than a dropped term
#: because a consumer that *can* wait would occupy it, and then the lead would grow by exactly this.
SINK_BLOCKS_HELD = 0


def check_golden_is_loggable(*bases: int) -> None:
    """Refuse a golden whose first sample is :data:`~waveflow.hw.rf_shot_tx.FILLER`.

    **The whole log rests on this**, and until S2 it was a convention in :func:`segments`' docstring
    rather than a check.  ``segments()`` splits on ``== FILLER``, so a waveform that *opened* with a
    zero would put every boundary one sample late.

    **Stronger than "the waveform has at least one non-zero sample", and the difference is the
    reason this exists.**  Both backends would make the identical error, so the two logs would still
    agree and :func:`compare_after_transients` would still pass -- alignment is robust to it.  What
    breaks is **coverage**: the leading zeros of every playout run would sit inside the filler run
    and never be compared at all.  A gate that silently stops looking at samples is worse than one
    that fails.

    Raises rather than asserts: it runs at import, and ``python -O`` strips an ``assert``.
    """
    for base in bases:
        first = int(shot_codes(base)[0])
        if first == FILLER:
            raise ValueError(
                f"the golden at base {base} starts on {first}, which is FILLER. segments() splits "
                f"the playout on FILLER, so the filler->play boundary would land after the leading "
                f"zeros -- identically in both backends, so the alignment would survive and the "
                f"leading samples of every run would go silently uncompared.")


check_golden_is_loggable(*KNOWN_BASES)


def c_lead(tb: "RfShotTxTB", *, sink_blocks_held: int = SINK_BLOCKS_HELD) -> int:
    """The **lead** in samples: how far ahead of the converter's grid the player may get.

    Derived from the declared depths on the bound interface graph, never chosen -- so it tracks when
    ``blk_words`` or a queue depth changes, which is the whole reason `plans/lt_transient.md`
    insisted the bound be derived.  Every term is sourced in that plan's *S1 as measured*, and S1
    measured the total at **exactly** this value with each stage at its declared maximum.

    ``max(depth, blk_words)`` is not a flourish.  `interface._admit_blocking` has two regimes: a
    burst that fits waits for room and the channel's capacity is its depth, while a burst **larger
    than the whole queue** waits for the queue to empty and then goes in whole -- one burst in
    flight.  The composite's own ``samp`` channel is declared depth 2 against a 16-word burst, so
    its capacity is 16 words and not 2; a formula that read ``depth`` and stopped there would
    understate that term eightfold.

    What this does **not** bound is the transient.  The startup transient also carries the design's
    own load latency -- driver, header, payload, lock acquire, grant -- expressed on the converter's
    grid, and no queue depth is an input to it.  See :func:`transients`.
    """
    spw = int(tb.word.samp_per_word)
    blk = int(tb.blksize)
    bw = int(tb.dut.play.blk_words)
    samp_if = tb.dut.interfaces[f"{tb.dut.name}_samp_if"]
    return (
        spw * max(int(samp_if.depth), bw)          # 1  the composite's own `samp` FIFO
        + spw * int(tb.dut.relayout.blk_words)     # 2  the re-layout task's in-flight burst
        + spw * max(int(tb.dac_axis.depth), bw)    # 3  the testbench's `dac` FIFO
        + blk                                      # 4  the Rfdc's DAC process, in-flight block
        + blk * int(tb.dac_if.depth)               # 5  RFSampIF's producer-side buffer
        + blk * int(sink_blocks_held)              # 6  the sink's own queue -- zero, and measured
    )


def check_horizon_covers_the_lead(tb: "RfShotTxTB", *, where: str = "") -> None:
    """:data:`N_BLK` still follows from :func:`c_lead` — the other half of "derived, not chosen".

    The horizon is a field default of the very testbench :func:`c_lead` reads, so it cannot be
    computed at import without circularity.  It is written down instead, and this is what keeps the
    two honest: **one formula, in `c_lead`, and a check that the recorded number still follows.**

    A horizon that no longer covers the lead does not fail loudly on its own — the run simply stops
    with part of the playout still in the pipe, and the capture looks like a design that was
    truncated.  That is the failure this exists to name.
    """
    import math

    want = int(N_BLK_BASE) + math.ceil(c_lead(tb) / int(tb.blksize))
    if int(tb.n_blk) != want:
        raise AssertionError(
            f"{where}the horizon is {int(tb.n_blk)} blocks; c_lead is {c_lead(tb)} samples, so it "
            f"should be N_BLK_BASE ({N_BLK_BASE}) + ceil({c_lead(tb)} / {int(tb.blksize)}) = {want}. "
            f"A depth on the path changed and the horizon did not follow: the run would end with "
            f"part of the playout still in flight, which reads as a truncated design.")


def play_log(played: np.ndarray) -> list[PlayRun]:
    """The playout runs of *played*, in order — **the log both gates align on**.

    Derived from the stream itself: :func:`segments` already splits a playout into
    ``(is_filler, samples)`` runs, and the filler->play transitions *are* the log.  No VCD, no
    cycle-to-sample mapping, and both backends go through this same function -- so neither can be
    aligned by something the other does not have.

    It works only because the golden excludes ``FILLER``; :func:`check_golden_is_loggable` is that
    dependency made into a check.
    """
    out: list[PlayRun] = []
    i = 0
    for is_filler, seg in segments(played):
        if not is_filler:
            out.append(PlayRun(event=int(seg[0]), start=i, length=int(seg.size)))
        i += int(seg.size)
    return out


def check_phase(played: np.ndarray, *, where: str = "") -> None:
    """**Gate 1.**  Every playout run is the loaded waveform, in phase, from its own beginning.

    ``real[i] == shot_codes(base)[i % nsamp]`` within each run -- a read-pointer error, a wrap error
    or an off-by-one at the region boundary all break it, and **an LT transient cannot**: this gate
    does not care when playing started, only what came out once it did.  That is what makes relaxing
    the cross-backend comparison safe rather than merely convenient.

    **Per segment, not per run of the whole capture.**  `RfShotTx` restarts the shot from its
    beginning after a preemption, so a whole-capture assertion would fail for the right reason and
    look like a bug.

    **Per backend.**  It is not a comparison against the other one: `array_equal` against the other
    backend says only that they differ, while this says *which* has the wrong phase.

    Two things it catches that :func:`check_finite_playout` and :func:`check_loop_playout` do not:
    it runs on the **pysim** capture as well as the RTL one, and it covers the ragged **tail** --
    those two truncate to whole passes (``whole = size - size % want.size``) and never look at a
    final partial pass the horizon cut.
    """
    for k, (is_filler, seg) in enumerate(segments(played)):
        if is_filler or seg.size == 0:
            continue
        base = int(seg[0])
        if base not in KNOWN_BASES:
            raise AssertionError(
                f"{where}playout run {k} starts on code {base}, which is not one of the loaded "
                f"waveforms {list(KNOWN_BASES)}. A run that begins mid-waveform is a read pointer "
                f"that did not wrap to the region's start.")
        want = shot_codes(base)
        got_want = want[np.arange(seg.size) % want.size]
        if not np.array_equal(seg, got_want):
            i = int(np.flatnonzero(seg != got_want)[0])
            raise AssertionError(
                f"{where}playout run {k} (base {base}, {seg.size} samples) is out of phase at "
                f"sample {i}: played {int(seg[i])}, the waveform's sample {i % want.size} is "
                f"{int(got_want[i])}.")


def compare_after_transients(a: np.ndarray, log_a: list[PlayRun],
                             b: np.ndarray, log_b: list[PlayRun],
                             *, guard: int = 0, where: str = "",
                             names: tuple[str, str] = ("a", "b")) -> None:
    """**Gate 2.**  The two captures, aligned on their **own** logs, compared **exactly**.

    Startup and handover are the same case here: each is a filler->play transition, and each stream
    is segmented at its own.  The LT lead is then a constant prefix the alignment removes, which is
    why retiring the player's metronome does not weaken this.

    **The event sequence is asserted first, and that is load-bearing.**  Two runs that saw
    *different events* -- a different number of playouts, or the same number of different waveforms
    -- is a real divergence, and aligning them anyway would paper over exactly the failure worth
    catching.

    **Exact, not a tolerance.**  These are integer converter codes computed from one golden on both
    sides; any difference is a defect and an MSE would hide a single wrong sample.

    **This is the successor to ``test_both_backends_agree_sample_for_sample``, and it inherits that
    test's other claim.**  The old gate was the honest half of
    ``test_the_handover_leaves_a_speculative_read_that_the_design_discards``: pysim takes the region
    out of the owner's hands inside ``grant()`` and RAISES on the very next access, so if the RTL
    player were *using* the words it speculatively reads while yielded, the two sequences could not
    agree.  This gate still compares values after alignment, so that proof survives intact -- the
    alignment moves *where* the comparison starts, never *what* it compares.

    ``guard`` samples are skipped at the start of each run.  It defaults to **0** because S1
    measured 0 to be sufficient in both scenarios, on all 1088 samples after the first transition.
    It stays a parameter as insurance against a geometry where the boundary lands a sample or two
    off -- not as cover for the lead, which the log already removes.
    """
    ea, eb = [r.event for r in log_a], [r.event for r in log_b]
    if ea != eb:
        raise AssertionError(
            f"{where}the two runs saw different events: {names[0]} played {ea}, {names[1]} played "
            f"{eb}. Different events is a divergence in its own right -- aligning them anyway would "
            f"compare two things that did not happen in the same order.")
    for k, (ra, rb) in enumerate(zip(log_a, log_b)):
        n = min(ra.length, rb.length) - int(guard)
        if n <= 0:
            continue
        sa = a[ra.start + guard:ra.start + guard + n]
        sb = b[rb.start + guard:rb.start + guard + n]
        if not np.array_equal(sa, sb):
            i = int(np.flatnonzero(sa != sb)[0])
            raise AssertionError(
                f"{where}playout run {k} (event {ra.event}) differs at sample {i} of {n}: "
                f"{names[0]}[{ra.start + guard + i}] = {int(sa[i])}, "
                f"{names[1]}[{rb.start + guard + i}] = {int(sb[i])}. The runs are aligned on their "
                f"own filler->play boundaries, so this is a value difference and not a timing one.")


def transients(played: np.ndarray) -> tuple[int, list[int]]:
    """``(startup, handovers)`` in samples — **the measurement gate 3 pins**.

    Two quantities and **not one**, because they are not the same thing (S1):

    * the **startup** transient is the design's load latency *plus* the LT lead, so it differs
      between a backend paced by ``TREADY`` and one paced by back-pressure alone;
    * a **handover** is pure latency.  The lead is built once, and by the time a handover happens
      the pipe is already full -- so it is identical in both backends and under either pacing.

    A trailing filler run is neither: it is the design going quiet on purpose, and its length is set
    by the horizon.  Only filler runs with a playout on *both* sides are handovers.
    """
    segs = segments(played)
    startup = int(segs[0][1].size) if segs and segs[0][0] else 0
    handovers = [int(s.size) for k, (f, s) in enumerate(segs)
                 if f and 0 < k < len(segs) - 1]
    return startup, handovers


def check_responses(got, frames, *, where: str = "") -> None:
    """Every header answered, in order, with its own ``tid`` and the right verdict."""
    want = expected_responses(frames)
    if got != want:
        def fmt(rs):
            return [(t, SHOT_STATUS_NAMES.get(s, s), n) for t, s, n in rs]
        raise AssertionError(f"{where}responses {fmt(got)}, expected {fmt(want)}")


def check_finite_playout(played: np.ndarray, *, where: str = "") -> None:
    """**Gate 1 + 3.**  Three passes of waveform A, bit-exact, and then quiet.

    A design that preempted the running shot would produce *two* passes — a perfectly good shorter
    signal that every counter downstream still adds up for — so the pass count is the assertion and
    the trailing filler is what says it stopped on purpose.
    """
    want = shot_codes(CODE_A)
    runs = [s for f, s in segments(played) if not f]
    if len(runs) != 1:
        raise AssertionError(
            f"{where}the playout has {len(runs)} non-filler run(s), expected 1: a finite shot is "
            f"one continuous run of passes between the startup filler and the tail. Segments: "
            f"{[(bool(f), int(s.size)) for f, s in segments(played)]}")
    got = runs[0]
    if got.size != NREPEAT * want.size:
        raise AssertionError(
            f"{where}the run is {got.size} samples, expected {NREPEAT * want.size} — "
            f"{NREPEAT} whole passes. A truncated play carries the right samples as far as it got.")
    if not np.array_equal(got.reshape(NREPEAT, want.size), np.tile(want, (NREPEAT, 1))):
        raise AssertionError(f"{where}the passes are not copies of the loaded waveform")
    if not segments(played)[-1][0]:
        raise AssertionError(f"{where}the run did not end in filler — the player never went quiet")


def check_loop_playout(played: np.ndarray, *, where: str = "") -> None:
    """**Gate 2.**  Waveform A, a gap, waveform B — then quiet, because the last load was short.

    The trailing filler is the merged design's own improvement: the infinite predecessor played a
    padded short shot because it had no way to go quiet, and this one does.
    """
    want_a, want_b = shot_codes(CODE_A), shot_codes(CODE_B)
    runs = [s for f, s in segments(played) if not f]
    if len(runs) != 2:
        raise AssertionError(
            f"{where}the playout has {len(runs)} non-filler run(s), expected 2: waveform A, a "
            f"handover gap, then waveform B. Segments: "
            f"{[(bool(f), int(s.size)) for f, s in segments(played)]}")
    for want, got, which in ((want_a, runs[0], "A"), (want_b, runs[1], "B")):
        n = min(int(got.size), int(want.size))
        if n == 0 or not np.array_equal(got[:n], want[:n]):
            raise AssertionError(f"{where}waveform {which} is not what was loaded")
        whole = int(got.size) - (int(got.size) % int(want.size))
        if whole and not np.array_equal(got[:whole].reshape(-1, want.size),
                                        np.tile(want, (whole // want.size, 1))):
            raise AssertionError(
                f"{where}waveform {which} does not repeat from its own start; the read pointer is "
                f"not wrapping to the region's beginning")
    if not segments(played)[-1][0]:
        raise AssertionError(
            f"{where}the run did not end in filler — the short shot reached the converter, which is "
            f"the one thing a truncated transfer must not do")
