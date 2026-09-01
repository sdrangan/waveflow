"""rf_shot_loop.py — ``plans/t2p_lock_chan.md`` S1: **change the waveform without stopping the DAC**.

The same lab story ``examples/rf_shot_play`` tells, minus the thing that made it awkward.  There, a
load arriving while a shot is playing is refused with
:data:`~waveflow.hw.rf_shot_tx.SHOT_BUSY` — the memory is under a live reader and there is no way to
say *stop touching it* — so changing waveform means waiting for the play-set to finish.  Here the
player is the **owner** of a :class:`~waveflow.hw.locked_mem.LockedT2pMemIF`, so it can be told, and
the design plays filler for exactly as long as the handover takes::

    StreamDriver --[ShotTxHdr | dense words ... TLAST]--> RfShotTxLoop.s_in
    RfShotTxLoop.resp_out --> StreamSink                (one ShotTxResp per header)
    RfShotTxLoop.samp_out --> Rfdc.tx_streams[0] | Rfdc.tx_rf --RFSampIF--> RfDataSink

**The converter is really here**, for ``rf_shot_play``'s reason: the one thing a playout design
exists to satisfy is that a DAC cannot be told to wait, and the whole claim of this design is that a
*handover* does not make it wait either — it makes it play silence, briefly, and then a different
waveform.  The tile is DAC-only (``n_rx=0``): wiring a fake ADC in would add a metronome nothing
drains.

**The region is at the top of the memory, and that is the gate.**  ``base = depth - nword``, so the
last element the design ever touches is the memory's last.  ``base + offset`` is the shape of the
byte-versus-word bug ``bram_toy`` stayed green through: consistently mis-scaled addressing round-trips
perfectly right up to the point its memory wraps, so a build that only ever loaded at zero would be
measuring nothing.

One scenario, and why it can be one
-----------------------------------
``rf_shot_play`` needs two, because once a shot is accepted its buffer is busy and **at most one load
per stream can succeed**.  That constraint is exactly what this design removes, so a single
file-driven stream reaches every verdict *and* both loads::

    tid 0   a whole shot                          SHOT_LOADED   -> plays
    tid 1   nsamp the design was not built for    SHOT_WRONG_LEN
    tid 2   nsamp == 0, no payload at all         SHOT_ZERO_LEN
    tid 3   a SHOT_LOAD -- a finite play          SHOT_WRONG_LEN
    tid 4   a second whole shot, mid-play         SHOT_LOADED   -> the waveform SWITCHES
    tid 5   SHOT_END -- the fence                 SHOT_LOADED

``tid`` 1 and 2 sit between the two loads deliberately: their payloads have to be drained, which
gives the first waveform airtime on the converter before the second arrives.  Without them the two
loads would be back to back and the switch would happen before the first shot had played a block.
``tid`` 3 is the one that would be invisible if it were wrong — a ``SHOT_LOAD`` asks for a finite
number of plays, which this design cannot provide, and reinterpreting it as a loop would produce
perfect-looking samples.
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
from waveflow.hw.rf_shot_loop import FILLER, RfShotTxLoop, ShotLoopPlay
from waveflow.hw.rf_shot_tx import (
    SHOT_END,
    SHOT_LOAD,
    SHOT_LOADED,
    SHOT_LOOP,
    SHOT_STATUS_NAMES,
    SHOT_WRONG_LEN,
    SHOT_ZERO_LEN,
    ShotTxHdr,
)
from waveflow.hw.rfdc_samp_word import Rfsoc4x2SampWord
from waveflow.simulation.rf_tb import RfDataSink
from waveflow.simulation.simulation import Simulation
from waveflow.simulation.stream_tb import StreamDriver, StreamSink

from examples.rf_loopback.rfdc import Rfdc

HERE = Path(__file__).resolve().parent

#: The converter's word: four 14-in-16 samples in 64 bits.  ``justify_shift() == 2``, so the last
#: stage is a real conversion — a build with ``shift == 0`` would be measuring a pair of wires.
WORD = Rfsoc4x2SampWord.specialize(samp_per_word=4)

#: Words in one shot, and words the memory holds.
NWORD = 64
DEPTH = 256
#: First element of the region.  **The top of the memory** — see the module docstring.
BASE = DEPTH - NWORD

#: Samples per converter block, and the same number in words.  One number decides the player's chunk,
#: its poll period and the re-layout's pysim burst, because they are one boundary.
BLKSIZE = 64
BLK_WORDS = BLKSIZE // int(WORD.samp_per_word)

#: The DAC's sample rate, and the fabric's.  0.256 words per cycle, so the converter is the
#: bottleneck and a handover is visible as blocks rather than as cycles.
SAMP_RATE = 256e6

#: Converter blocks the metronome runs for.  Long enough that the first waveform plays, the handover
#: happens, and the second waveform plays — short enough that an xsim run is a minute.
N_BLK = 20

#: Fixed run bound for the generated XSI main — a testbench constant, not a latency.
#: ``N_BLK * BLK_WORDS`` words at 0.256 words/cycle, plus a tail for the last verdict to land.
XSI_N_CYCLES = 1400

#: Base sample codes for the two waveforms.  Far apart and non-overlapping, so "the output switched"
#: is decidable from any single sample rather than from a correlation.
CODE_A = 1000
CODE_B = 5000

#: Blocks the DAC may zero-fill before the first shot reaches it.  A converter fed through a pipeline
#: **must** zero-fill until data arrives; what must not happen is zero-filling afterwards, other than
#: for the handover this design exists to make possible.  **Measured** (2026-09-01), not predicted:
#: three blocks pass before the first load has crossed the loader, the memory and the re-layout.
STARTUP_BLOCKS = 3


# ---------------------------------------------------------------------------
# The waveforms
# ---------------------------------------------------------------------------

def shot_codes(base: int, nword: int = NWORD) -> np.ndarray:
    """``nword * samp_per_word`` distinguishable converter codes, as signed integers.

    A ramp rather than a constant, for the reason this repo keeps rediscovering: a constant payload
    cannot tell a word that landed at the wrong address from one that landed at the right one, and it
    cannot tell a play that restarted from one that continued.
    """
    n = int(nword) * int(WORD.samp_per_word)
    return np.arange(int(base), int(base) + n, dtype=np.int64)


def shot_slots(base: int, nword: int = NWORD) -> np.ndarray:
    """The same waveform as **converter words** — what the DAC is handed."""
    from waveflow.hw.rfdc_samp_word import pack

    return np.asarray(pack(WORD, shot_codes(base, nword).reshape(1, -1)),
                      dtype=np.uint64).ravel()


def shot_dense(base: int, nword: int = NWORD) -> np.ndarray:
    """The same waveform as **densely-packed** words — what a host writes.

    Dense on the wire and dense in the memory: the host does not need to know anything about
    justification, and the re-layout at the end of the chain owns the converter's packing.
    """
    return to_dense(WORD, shot_slots(base, nword))


# ---------------------------------------------------------------------------
# The scenario
# ---------------------------------------------------------------------------

def frame(opcode: int, tid: int, nsamp: int, payload: np.ndarray) -> np.ndarray:
    """One AXI-Stream **frame**: the header, then the payload, ``TLAST`` on the last word.

    A burst in the bundle *is* a frame — the pysim ``StreamDriver`` writes one burst per ``write``
    and the XSI ``AxisMaster`` raises ``TLAST`` on each burst's last beat — so the two backends carry
    the same boundary rather than two encodings of it.
    """
    h = ShotTxHdr()
    h.opcode, h.tid, h.nsamp, h.nrepeat = int(opcode), int(tid), int(nsamp), 1
    words = np.asarray(h.serialize(int(WORD.bitwidth)), dtype=np.uint64).ravel()
    return np.concatenate([words, np.asarray(payload, dtype=np.uint64).ravel()])


def nsamp_shot() -> int:
    """Samples in a whole shot — the one value ``ShotTxHdr.nsamp`` may carry."""
    return NWORD * int(WORD.samp_per_word)


def gate_frames() -> list[np.ndarray]:
    """The one scenario — see the module docstring for what each ``tid`` is for."""
    empty = np.zeros(0, dtype=np.uint64)
    full = nsamp_shot()
    return [
        frame(SHOT_LOOP, 0, full, shot_dense(CODE_A)),
        frame(SHOT_LOOP, 1, full + int(WORD.samp_per_word), shot_dense(CODE_A)),
        frame(SHOT_LOOP, 2, 0, empty),
        frame(SHOT_LOAD, 3, full, shot_dense(CODE_A)),
        frame(SHOT_LOOP, 4, full, shot_dense(CODE_B)),
        frame(SHOT_END, 5, 0, empty),
    ]


GATE_FRAMES = gate_frames()


def expected_responses(frames=None) -> list[tuple[int, int, int]]:
    """``(tid, status, nsamp_loaded)`` the design must produce, **derived from the frames**.

    Derived rather than transcribed: a scenario edited without its golden is how a gate comes to
    assert what the design happens to do.  The rules are :class:`ShotLoopLoad`'s, restated in the
    smallest form that can be read at a glance — and a disagreement is the finding.
    """
    frames = GATE_FRAMES if frames is None else frames
    w, spw, full = int(WORD.bitwidth), int(WORD.samp_per_word), nsamp_shot()
    hn = ShotTxHdr.nwords_per_inst(w)
    out: list[tuple[int, int, int]] = []
    for f in frames:
        h = ShotTxHdr().deserialize(np.asarray(f, dtype=np.uint64)[:hn], word_bw=w)
        took = min(int(np.asarray(f).size) - hn, NWORD)
        if int(h.opcode) == SHOT_END:
            out.append((int(h.tid), SHOT_LOADED, 0))
        elif int(h.opcode) != SHOT_LOOP:
            out.append((int(h.tid), SHOT_WRONG_LEN, 0))     # a finite play, refused not reinterpreted
        elif int(h.nsamp) == 0:
            out.append((int(h.tid), SHOT_ZERO_LEN, 0))
        elif int(h.nsamp) != full:
            out.append((int(h.tid), SHOT_WRONG_LEN, 0))
        else:
            out.append((int(h.tid), SHOT_LOADED, took * spw))
    return out


def expected_loads(frames=None) -> int:
    """How many shots reach the memory — one handover each."""
    return sum(1 for _t, s, n in expected_responses(frames) if s == SHOT_LOADED and n)


def write_scenario(root, frames=None, name: str = "cmd") -> None:
    """Materialize ``<root>/vectors/<name>`` — the frames BOTH backends drive in.

    One writer, so the RTL run and the pysim golden cannot start from different bytes; one burst per
    frame, so ``TLAST`` lands where the header said the payload ends.
    """
    from waveflow.utils.burst_io import write_burst_bundle

    write_burst_bundle(list(GATE_FRAMES if frames is None else frames),
                       Path(root) / "vectors" / name)


# ---------------------------------------------------------------------------
# The graph
# ---------------------------------------------------------------------------

@dataclass
class RfShotLoopTB(FreeRunMod):
    """A driver pushing frames, the looping transmitter, a real DAC, and two sinks.

    Structurally ``RfShotPlayTB`` with one fewer thing to arrange: there is no ``done`` token to wait
    for and no second scenario, because the design under test never becomes busy.
    """

    potential_targets: ClassVar[frozenset[str]] = frozenset({SEQUENTIAL_XSI_TB})

    #: The design under test.  **A ClassVar, changed by subclassing**, and it exists for the same
    #: reason :attr:`~waveflow.hw.rf_shot_loop.RfShotTxLoop.player_cls` does: the positive control
    #: needs its OWN generated harness, because the harness hardcodes ``DESIGN_DLL`` — the path of
    #: the elaborated snapshot it loads.  A control that ran against the shipped design's snapshot
    #: would report the shipped design's numbers and find no hazard, which is the single most
    #: convincing way for this gate to lie.
    dut_cls: ClassVar[type] = RfShotTxLoop

    nword: int = NWORD
    depth: int = DEPTH
    base: int = BASE
    blksize: int = BLKSIZE
    n_blk: int = N_BLK
    samp_rate: float = SAMP_RATE
    axis_freq: float = RFSOC4X2_CLK_HZ
    #: The converter's packing convention, as one type.  Everything downstream is read off it.
    word: type[Rfsoc4x2SampWord] = WORD
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
        self.dut = type(self).dut_cls.for_word(
            self.word, depth=int(self.depth), nword=int(self.nword), sim=self.sim,
            name=f"{self.name}_dut", clk=self.axis_clk, base=int(self.base),
            # One number for the player's chunk, its poll period and the re-layout's pysim burst:
            # they are one boundary.  pysim's quantum on the converter edge is a BLOCK.
            blk_words=int(self.blksize) // int(self.word.samp_per_word),
            # The metronome, handed over directly: pysim does not back-pressure a burst write, so
            # this is the only way the converter's rate reaches the player.
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
        """Simulated horizon: the metronome's own length plus a two-block tail.

        A testbench constant, not a latency.  The converter is a free-running event source that never
        exhausts — that is what a DAC does, and infinite play is the design saying the same thing —
        so ``env.run()`` with no bound would never return.
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

def run_pysim(root=None, frames=None, **kw) -> RfShotLoopTB:
    """Build the graph, run it to the metronome's horizon, return the testbench."""
    import tempfile

    tb = RfShotLoopTB(name="tb", sim=Simulation(), **kw)
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


def responses(tb: RfShotLoopTB) -> list[tuple[int, int, int]]:
    """``(tid, status, nsamp_loaded)`` in arrival order, read off the response **stream**.

    Off the wire rather than off the module's own list, because the wire is what a host sees and it
    is the half a counter cannot vouch for: a design that decided correctly and serialized wrongly
    passes every internal check.
    """
    from waveflow.hw.rf_shot_tx import ShotTxResp

    if not tb.resp_snk.words:
        return []
    words = np.concatenate([np.asarray(b).ravel() for b in tb.resp_snk.words])
    n = ShotTxResp.nwords_per_inst(int(WORD.bitwidth))
    out = []
    for i in range(0, words.size - n + 1, n):
        r = ShotTxResp().deserialize(words[i:i + n], word_bw=int(WORD.bitwidth))
        out.append((int(r.tid), int(r.status), int(r.nsamp_loaded)))
    return out


def blocks_to_codes(blocks) -> np.ndarray:
    """``(n_blk, 1, blksize)`` normalized RF blocks -> one flat array of signed converter codes."""
    from waveflow.hw.fixpoint import from_real

    arr = np.asarray(blocks)
    if arr.size == 0:
        return np.zeros(0, dtype=np.int64)
    samp_type = WORD.samp_type()
    return np.asarray(from_real(arr.reshape(-1), samp_type), dtype=np.int64)


def played_samples(tb: RfShotLoopTB) -> np.ndarray:
    """Everything the converter put on the air, as signed codes."""
    return blocks_to_codes(np.asarray(tb.sink.blocks))


def segments(played: np.ndarray) -> list[tuple[bool, np.ndarray]]:
    """The playout split into ``(is_filler, samples)`` runs.

    Filler is a run of :data:`~waveflow.hw.rf_shot_loop.FILLER` codes, and splitting on it is how the
    *shape* of a handover is read: a gap between two waveforms, rather than one waveform that happens
    to contain some zeros.  Both gate waveforms start at a non-zero code precisely so this is
    unambiguous.
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


def check_responses(got, frames=None, where: str = "") -> None:
    """Every header answered, in order, with its own ``tid`` and the right verdict."""
    want = expected_responses(frames)
    if got != want:
        def fmt(rs):
            return [(t, SHOT_STATUS_NAMES.get(s, s), n) for t, s, n in rs]
        raise AssertionError(f"{where}responses {fmt(got)}, expected {fmt(want)}")


def check_switched(played: np.ndarray, where: str = "") -> None:
    """**The gate.**  Waveform A, then filler, then waveform B — each bit-exact from its own start.

    Three separate claims, and each fails differently.  *A appeared* says the first load reached the
    converter at all.  *Filler between* says the handover was a real yield rather than an overwrite
    under a live reader — with no gap the samples would be a splice of two waveforms and every
    counter would still look right.  *B from its own beginning* says the player restarted at the
    region's first word: resuming mid-shot would put the tail of the old waveform's phase on the new
    one, which is right in no application and is invisible from a sample count.
    """
    want_a = shot_codes(CODE_A)
    want_b = shot_codes(CODE_B)
    runs = [s for is_filler, s in segments(played) if not is_filler]
    if len(runs) != 2:
        raise AssertionError(
            f"{where}the playout has {len(runs)} non-filler run(s), expected 2: waveform A, a "
            f"handover gap, then waveform B. Runs of "
            f"{[int(s.size) for _f, s in segments(played)]} samples "
            f"({[bool(f) for f, _s in segments(played)]} filler).")
    for want, got, which in ((want_a, runs[0], "A"), (want_b, runs[1], "B")):
        n = min(int(got.size), int(want.size))
        if n == 0 or not np.array_equal(got[:n], want[:n]):
            bad = int(np.argmax(got[:n] != want[:n])) if n else 0
            raise AssertionError(
                f"{where}waveform {which} differs from what was loaded at sample {bad}: "
                f"{got[:n][bad] if n else '(nothing)'} != {want[:n][bad] if n else '(nothing)'}. "
                f"The run is {int(got.size)} samples long.")
        # A play that stopped part way carries the right samples as far as it got, so ALIGNMENT is
        # what says the loop is a loop: every pass starts at the region's first word.
        whole = int(got.size) - (int(got.size) % int(want.size))
        if whole and not np.array_equal(got[:whole].reshape(-1, want.size),
                                        np.tile(want, (whole // want.size, 1))):
            raise AssertionError(
                f"{where}waveform {which} does not repeat from its own start; the read pointer is "
                f"not wrapping to the region's beginning.")


# ---------------------------------------------------------------------------
# The positive control — the same design with one line missing
# ---------------------------------------------------------------------------

@dataclass
class ShotLoopPlayDirty(ShotLoopPlay):
    """The shipped player with the ``playing = 0`` before the grant removed.

    Body: ``src/shot_loop_play_dirty_task.h``, which is
    ``waveflow/build/shot_loop_play_task.h`` byte-for-byte except for that one line and a
    ``playing`` that starts at 1 (so the memory is already being read when the first ``ACQUIRE``
    arrives — the control has to collide on its **first** handover, not eventually).

    It exists because the RTL check for the collision this lock prevents is a **VCD scan**: XSI
    discards ``$error``, so ``bram_t2p.v``'s assertion cannot be heard, and a scan that finds nothing
    is indistinguishable from a scan bound to the wrong nets.  The clean run's *"no hazards"* is
    evidence only because the **same scan on the same manifest** finds hazards here.
    """

    cpp_kernel_name: ClassVar[str | None] = "shot_loop_play_dirty"

    def kernel_task(self):
        from waveflow.hw.mem_stream import KernelTask

        return KernelTask("shot_loop_play_dirty_task", "shot_loop_play_dirty_task.h",
                          ("lock", "samp_out"),
                          template_args=(int(self.bitwidth), int(self.depth), int(self.nword),
                                         int(self.base), int(self.blk_words)))


@dataclass
class RfShotTxLoopDirty(RfShotTxLoop):
    """:class:`~waveflow.hw.rf_shot_loop.RfShotTxLoop` with the broken player.

    **The same composite**, reached through ``player_cls`` rather than copied: a separately written
    broken design would exercise different nets and prove nothing about the shipped one.  Its top and
    wrapper get their own names, so both builds can live in one ``xsi/`` directory and one gate can
    run them back to back.
    """

    cpp_kernel_name: ClassVar[str | None] = "rf_shot_tx_loop_dirty"
    player_cls: ClassVar[type] = ShotLoopPlayDirty


@dataclass
class RfShotLoopDirtyTB(RfShotLoopTB):
    """The same testbench graph around the **positive control**.

    Identical in every respect except which snapshot its harness loads — which is exactly the point:
    the two runs differ in one line of one task body and in nothing else, so a hazard found in this
    one is attributable to that line.
    """

    dut_cls: ClassVar[type] = RfShotTxLoopDirty
