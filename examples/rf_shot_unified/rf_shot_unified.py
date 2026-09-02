"""rf_shot_unified.py — ``plans/rf_shot_unify.md`` Stage A: **one transmitter, both play modes**.

The user story ``examples/rf_shot_play`` and ``examples/rf_shot_loop`` tell *between* them, told once::

    StreamDriver --[ShotTxHdr | dense words ... TLAST]--> RfShotTxUnified.s_in
    RfShotTxUnified.resp_out --> StreamSink              (one ShotTxResp per header)
    RfShotTxUnified.samp_out --> Rfdc.tx_streams[0] | Rfdc.tx_rf --RFSampIF--> RfDataSink

**Two scenarios, and they cannot be one.**  A file-driven driver pushes every frame back to back, and
a *finite* shot in flight refuses everything behind it — which is the design working, not a testbench
limitation.  So the finite behaviours and the infinite ones need separate streams, exactly as
``rf_shot_play`` needed two for its own reason:

``cmd_finite`` — ``SHOT_LOAD`` with ``nrepeat=3``, then three frames that arrive mid-play

============  ==========================================  ================
``tid`` 0     a whole shot, three passes                  ``SHOT_LOADED``
``tid`` 1     another load, arriving mid-play             ``SHOT_BUSY``
``tid`` 2     ``nsamp`` the design was not built for      ``SHOT_WRONG_LEN``
``tid`` 3     ``nsamp == 0``, and no payload at all       ``SHOT_ZERO_LEN``
``tid`` 4     ``SHOT_END`` — the fence                    ``SHOT_LOADED``
============  ==========================================  ================

``cmd_loop`` — ``SHOT_LOOP``, switched mid-play, then a short one

============  ==========================================  ================
``tid`` 0     waveform A, played forever                  ``SHOT_LOADED``
``tid`` 1     ``nsamp`` wrong — **and its payload drains** ``SHOT_WRONG_LEN``
``tid`` 2     ``nsamp == 0``                              ``SHOT_ZERO_LEN``
``tid`` 3     waveform B, **preempting** A                ``SHOT_LOADED``
``tid`` 4     a truncated transfer                        ``SHOT_SHORT``
``tid`` 5     ``SHOT_END``                                ``SHOT_LOADED``
============  ==========================================  ================

``tid`` 1 and 2 sit between the two loop loads deliberately: their payloads have to be drained, which
buys waveform A airtime on the converter before B arrives.  Without them the switch would happen
before A had played a block.

**The converter is really here**, because the one thing a playout design exists to satisfy is that a
DAC cannot be told to wait — and the claim of *both* halves is that neither a handover nor the end of
a finite shot makes it wait: it gets filler, on time, as real beats.

**The region is at the top of the memory** (``base = depth - nword``): ``base + offset`` is the shape
of the byte-versus-word bug ``bram_toy`` stayed green through, so a build that only ever loaded at
zero would be measuring nothing.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

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
    SHOT_SHORT,
    SHOT_STATUS_NAMES,
    SHOT_WRONG_LEN,
    SHOT_ZERO_LEN,
    ShotTxHdr,
)
from waveflow.hw.rf_shot_tx_unified import FILLER, RfShotTxUnified
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

#: Words in one shot, words the memory holds, and where the shot sits — at the **top**.
NWORD = 64
DEPTH = 256
BASE = DEPTH - NWORD
NSAMP = NWORD * SPW

#: Samples per converter block, and the same number in words.
BLKSIZE = 64
BLK_WORDS = BLKSIZE // SPW

#: The DAC's sample rate against a 250 MHz fabric: 0.256 words per cycle.
SAMP_RATE = 256e6

#: Passes the finite scenario asks for.
NREPEAT = 3

#: Converter blocks the metronome runs for, and the XSI main's fixed run bound.  A testbench
#: constant, not a latency: the converter never exhausts, so an unbounded run would not return.
N_BLK = 20
XSI_N_CYCLES = 1400

#: Base sample codes for the two waveforms.  Far apart and non-overlapping, so "the output switched"
#: is decidable from any single sample.
CODE_A = 1000
CODE_B = 5000
#: Words in the deliberately truncated transfer.
SHORT_WORDS = NWORD // 2


# ---------------------------------------------------------------------------
# The waveforms
# ---------------------------------------------------------------------------

def shot_codes(base: int, nword: int = NWORD) -> np.ndarray:
    """``nword * samp_per_word`` distinguishable converter codes, as signed integers."""
    return np.arange(int(base), int(base) + int(nword) * SPW, dtype=np.int64)


def shot_slots(base: int, nword: int = NWORD) -> np.ndarray:
    """The same waveform as **converter words** — what the DAC is handed."""
    from waveflow.hw.rfdc_samp_word import pack

    return np.asarray(pack(WORD, shot_codes(base, nword).reshape(1, -1)), dtype=np.uint64).ravel()


def shot_dense(base: int, nword: int = NWORD) -> np.ndarray:
    """The same waveform as **densely-packed** words — what a host writes.

    Dense on the wire and dense in the memory: the host needs to know nothing about justification,
    and the re-layout at the end of the chain owns the converter's packing.
    """
    return to_dense(WORD, shot_slots(base, nword))


# ---------------------------------------------------------------------------
# The scenarios
# ---------------------------------------------------------------------------

def frame(opcode: int, tid: int, nsamp: int, nrepeat: int, payload: np.ndarray) -> np.ndarray:
    """One AXI-Stream **frame**: the header, then the payload, ``TLAST`` on the last word.

    A burst in the bundle *is* a frame — the pysim ``StreamDriver`` writes one burst per ``write``
    and the XSI ``AxisMaster`` raises ``TLAST`` on each burst's last beat — so the two backends carry
    the same boundary rather than two encodings of it.
    """
    h = ShotTxHdr()
    h.opcode, h.tid, h.nsamp, h.nrepeat = int(opcode), int(tid), int(nsamp), int(nrepeat)
    return np.concatenate([np.asarray(h.serialize(word_bw=WORD_BW), dtype=np.uint64).ravel(),
                           np.asarray(payload, dtype=np.uint64).ravel()])


def finite_frames() -> list[np.ndarray]:
    """The finite scenario — see the module docstring."""
    a = shot_dense(CODE_A)
    empty = np.zeros(0, dtype=np.uint64)
    return [
        frame(SHOT_LOAD, 0, NSAMP, NREPEAT, a),
        frame(SHOT_LOAD, 1, NSAMP, 1, a),
        frame(SHOT_LOAD, 2, NSAMP + SPW, 1, a),
        frame(SHOT_LOOP, 3, 0, 1, empty),
        frame(SHOT_END, 4, 0, 0, empty),
    ]


def loop_frames() -> list[np.ndarray]:
    """The infinite scenario — see the module docstring."""
    a, b = shot_dense(CODE_A), shot_dense(CODE_B)
    empty = np.zeros(0, dtype=np.uint64)
    return [
        frame(SHOT_LOOP, 0, NSAMP, 1, a),
        frame(SHOT_LOOP, 1, NSAMP + SPW, 1, a),
        frame(SHOT_LOOP, 2, 0, 1, empty),
        frame(SHOT_LOOP, 3, NSAMP, 1, b),
        frame(SHOT_LOOP, 4, NSAMP, 1, b[:SHORT_WORDS]),
        frame(SHOT_END, 5, 0, 0, empty),
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

    ``busy`` is modelled the way the design has it: set by an accepted **finite** load, and cleared
    only when that shot's passes are over.  Within a back-to-back scenario nothing finishes in time,
    so once set it stays set — which is exactly why the two scenarios are two.
    """
    hn = ShotTxHdr.nwords_per_inst(WORD_BW)
    out: list[tuple[int, int, int]] = []
    busy = False
    for f in frames:
        h = ShotTxHdr().deserialize(np.asarray(f, dtype=np.uint64)[:hn], word_bw=WORD_BW)
        took = min(int(np.asarray(f).size) - hn, NWORD)
        op = int(h.opcode)
        if op == SHOT_END:
            out.append((int(h.tid), SHOT_LOADED, 0))
        elif op not in (SHOT_LOAD, SHOT_LOOP):
            out.append((int(h.tid), SHOT_WRONG_LEN, 0))
        elif int(h.nsamp) == 0:
            out.append((int(h.tid), SHOT_ZERO_LEN, 0))
        elif int(h.nsamp) != NSAMP:
            out.append((int(h.tid), SHOT_WRONG_LEN, 0))
        elif busy:
            out.append((int(h.tid), SHOT_BUSY, 0))
        else:
            out.append((int(h.tid), SHOT_LOADED if took == NWORD else SHOT_SHORT, took * SPW))
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
class RfShotUnifiedTB(FreeRunMod):
    """A driver pushing frames, the unified transmitter, a real DAC, and two sinks."""

    potential_targets: ClassVar[frozenset[str]] = frozenset({SEQUENTIAL_XSI_TB})

    nword: int = NWORD
    depth: int = DEPTH
    base: int = BASE
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
        self.dut = RfShotTxUnified.for_word(
            self.word, depth=int(self.depth), nword=int(self.nword), sim=self.sim,
            name=f"{self.name}_dut", clk=self.axis_clk, base=int(self.base),
            # pysim's quantum on the converter edge is a BLOCK: the Rfdc's DAC process takes one
            # blksize burst per event and refuses a partial one.  A modelling shape only.
            blk_words=int(self.blksize) // SPW,
            # The metronome, handed over directly: pysim does not back-pressure a burst write, so
            # this is the only way the converter's rate reaches the player.
            dac_word_rate=float(self.samp_rate) / SPW)
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
        # No depth overrides on the three that become the DUT's own boundary ports: a top-level AXIS
        # argument cannot carry a FIFO depth (Vitis ignores the pragma, sometimes silently).
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

def run_pysim(root=None, frames=None, **kw) -> RfShotUnifiedTB:
    """Build the graph, run it to the metronome's horizon, return the testbench."""
    import tempfile

    tb = RfShotUnifiedTB(name="tb", sim=Simulation(), **kw)
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


def responses(tb: RfShotUnifiedTB) -> list[tuple[int, int, int]]:
    """``(tid, status, nsamp_loaded)`` in arrival order, read off the response **stream**.

    Off the wire rather than off the module's own list, because the wire is what a host sees and it
    is the half a counter cannot vouch for: a design that decided correctly and serialized wrongly
    passes every internal check.
    """
    from waveflow.hw.rf_shot_tx import ShotTxResp

    if not tb.resp_snk.words:
        return []
    words = np.concatenate([np.asarray(b).ravel() for b in tb.resp_snk.words])
    n = ShotTxResp.nwords_per_inst(WORD_BW)
    out = []
    for i in range(0, words.size - n + 1, n):
        r = ShotTxResp().deserialize(words[i:i + n], word_bw=WORD_BW)
        out.append((int(r.tid), int(r.status), int(r.nsamp_loaded)))
    return out


def blocks_to_codes(blocks) -> np.ndarray:
    """``(n_blk, 1, blksize)`` normalized RF blocks -> one flat array of signed converter codes."""
    from waveflow.hw.fixpoint import from_real

    arr = np.asarray(blocks)
    if arr.size == 0:
        return np.zeros(0, dtype=np.int64)
    return np.asarray(from_real(arr.reshape(-1), WORD.samp_type()), dtype=np.int64)


def played_samples(tb: RfShotUnifiedTB) -> np.ndarray:
    """Everything the converter put on the air, as signed codes."""
    return blocks_to_codes(np.asarray(tb.sink.blocks))


def segments(played: np.ndarray) -> list[tuple[bool, np.ndarray]]:
    """The playout split into ``(is_filler, samples)`` runs.

    Filler is a run of :data:`~waveflow.hw.rf_shot_tx_unified.FILLER` codes.  Both waveforms start at
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

    The trailing filler is the merged design's own improvement: ``rf_shot_loop`` plays a padded short
    shot because it has no way to go quiet, and this one does.
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
