"""rf_shot_rx.py — ``plans/t2p_lock_chan.md`` S2: **capture continuously and lose nothing**.

The RX half of the story ``examples/rf_shot_tx`` tells on TX.  There the two regions were an
optimisation nobody needed — a handover is a *gap*, and you had already accepted discontinuity when
you asked to change waveform.  Here they are correctness: **you cannot back-pressure an ADC**, so a
reader holding the region the capture needs is not a gap, it is samples that no longer exist::

    RfDataSource --RFSampIF--> Rfdc.rx_rf | Rfdc.rx_streams[0] --> RfShotRx.samp_in
    RfShotRx.w_out --> StreamSink        (one FRAME per window: a header, then the samples)

**The converter is really here**, for the TX design's reason inverted: the one thing a capture
design exists to satisfy is that an ADC cannot be told to wait, and the whole claim of this design is
that a *window read-out* does not make it wait either.  The tile is ADC-only (``n_rx=1, n_tx=0``):
wiring a fake DAC in would add a metronome nothing feeds.

**There is no command stream, and that is the design rather than an omission.**  A capture is asked
nothing — it is told *when a region is ready* by the design itself, and it answers on every window
with a header a host can act on.  Compare ``examples/rf_samp_buf_rx``, whose whole middle is a
command layer, because *its* reader has to say which window it wants.

The scenario is a ramp, and that is the gate
--------------------------------------------
The source plays a ramp of converter codes, so the windows the host receives must **concatenate into
a contiguous ramp**.  A dropped block is a *step* in the numbers — visible whether or not anything
counted it — which is what makes "nothing was lost" checkable rather than merely reported.  The
header's ``n_dropped`` and ``status`` are asserted too, because the two agreeing is what says the
design knows what it lost.
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
from waveflow.hw.rf_shot_rx import (
    CAP_OK,
    CAP_STATUS_NAMES,
    N_REGION,
    RfShotRx,
    split_windows,
)
from waveflow.hw.rf_sample_if import RFSampIF
from waveflow.hw.rfdc_samp_word import Rfsoc4x2SampWord
from waveflow.simulation.rf_tb import RfDataSource
from waveflow.simulation.simulation import Simulation
from waveflow.simulation.stream_tb import StreamSink

from examples.rf_loopback.rfdc import Rfdc

HERE = Path(__file__).resolve().parent

#: The converter's word: four 14-in-16 samples in 64 bits.  ``justify_shift() == 2``, so the first
#: stage is a real conversion rather than a pair of wires — the condition a gate has to hold or it is
#: measuring nothing.
WORD = Rfsoc4x2SampWord.specialize(samp_per_word=4)
WORD_BW = int(WORD.bitwidth)
SPW = int(WORD.samp_per_word)

#: Memory depth in **WORDS**, split into :data:`~waveflow.hw.rf_shot_rx.N_REGION` regions.
DEPTH = 256
REGION_WORDS = DEPTH // N_REGION
#: Samples in one window — what a host gets per frame.
REGION_SAMPLES = REGION_WORDS * SPW

#: Samples per converter block, and the same number in words.  One number decides the re-layout's
#: pysim burst, the capture's chunk, its poll period and the reader's output burst.
BLKSIZE = 64
BLK_WORDS = BLKSIZE // SPW

#: The ADC's sample rate against a 250 MHz fabric: 0.256 words per cycle, so the converter is the
#: bottleneck and the capture keeps up with room to spare — which is the point, since the design's
#: claim is that the *reader* is what would lose samples, not the datapath.
SAMP_RATE = 256e6

#: Converter blocks the metronome runs for.  Long enough to fill several regions and hand out several
#: windows — short enough that an xsim run is a minute.
N_BLK = 40

#: Fixed run bound for the generated XSI main — a testbench constant, not a latency.
#: ``N_BLK * BLK_WORDS`` words at 0.256 words/cycle, plus a tail for the last window to land.
XSI_N_CYCLES = 2800

#: Amplitude the ramp is normalised against.  Sending an INTEGER code scaled into [-1, 1) means the
#: converter's quantizer round-trips it exactly, so what the memory holds is the ramp itself and a
#: captured sample names its own index.
SAMP_BW = int(WORD.bits_per_samp)
#: Where the ramp starts.  Non-zero and well inside the code range, so a sample that came back as 0
#: is visibly *not* a sample rather than plausibly the first one.
CODE_BASE = 1000


# ---------------------------------------------------------------------------
# The scenario
# ---------------------------------------------------------------------------

def ramp_codes(n_samp: int) -> np.ndarray:
    """*n_samp* distinguishable converter codes.

    A ramp rather than anything cleverer, because the property under test is **contiguity**: a
    dropped block is a step in the numbers, and nothing else about the data has to be believed.
    """
    return np.arange(CODE_BASE, CODE_BASE + int(n_samp), dtype=np.int64)


def write_scenario(root, n_blk: int = N_BLK) -> None:
    """Materialize ``<root>/vectors/rf_in`` — the samples BOTH backends play.

    One writer, so the RTL run and the pysim golden cannot start from different bytes.
    """
    from waveflow.simulation.rf_tb import write_rf_bundle

    codes = ramp_codes(int(n_blk) * BLKSIZE)
    full = float(1 << (SAMP_BW - 1))
    blocks = [np.asarray(codes[i * BLKSIZE:(i + 1) * BLKSIZE], dtype=float).reshape(1, -1) / full
              for i in range(int(n_blk))]
    write_rf_bundle(blocks, Path(root) / "vectors" / "rf_in")


# ---------------------------------------------------------------------------
# The graph
# ---------------------------------------------------------------------------

@dataclass
class RfShotRxTB(FreeRunMod):
    """A real ADC filling the memory, the ping-pong receiver, and one sink taking windows.

    Structurally ``RfSampBufRxTB`` with the command layer removed — which is the comparison worth
    making: that design needs a host to ask for a window, and this one hands them over as they come.
    """

    potential_targets: ClassVar[frozenset[str]] = frozenset({SEQUENTIAL_XSI_TB})

    depth: int = DEPTH
    blksize: int = BLKSIZE
    n_blk: int = N_BLK
    samp_rate: float = SAMP_RATE
    axis_freq: float = RFSOC4X2_CLK_HZ
    #: The converter's packing convention, as one type.  Everything downstream is read off it.
    word: type[Rfsoc4x2SampWord] = WORD
    #: **Fault injection.**  Blocks' worth of time the reader sits on its window before releasing it.
    #: ``0`` is the design; anything else makes it lose samples on purpose — see
    #: :attr:`~waveflow.hw.rf_shot_rx.PingPongWindow.stall_blocks`.
    stall_blocks: int = 0
    #: Fixed run bound for the generated XSI main — a testbench constant, not a latency.
    n_cycles: int = XSI_N_CYCLES
    axis_clk: Clock = field(default_factory=lambda: Clock(freq=RFSOC4X2_CLK_HZ))

    def __post_init__(self) -> None:
        super().__post_init__()
        self.axis_clk = Clock(name=f"{self.name}_axis_clk", freq=float(self.axis_freq))
        self.samp_clk = Clock(name=f"{self.name}_samp_clk", freq=float(self.samp_rate))
        self.blk_period = int(self.blksize) / float(self.samp_rate)

        self.rfdc = Rfdc(name=f"{self.name}_rfdc", sim=self.sim, n_rx=1, n_tx=0, word=self.word)
        w = self.rfdc.axis_bitwidth
        self.dut = RfShotRx.for_word(
            self.word, depth=int(self.depth), sim=self.sim, name=f"{self.name}_dut",
            clk=self.axis_clk, blk_words=int(self.blksize) // SPW,
            stall_blocks=int(self.stall_blocks), blk_period=self.blk_period)
        self.source = RfDataSource(name=f"{self.name}_src", sim=self.sim, in_bundle="vectors/rf_in")
        self.win_snk = StreamSink(sim=self.sim, name=f"{self.name}_win_snk", bitwidth=w,
                                  out_bundle="vectors/win", has_tlast=True)
        for c in (self.dut, self.rfdc, self.source, self.win_snk):
            self.add_comp(c)

        # --- the RF domain: one interface, one metronome (there is no DAC) ----------------------
        self.adc_if = RFSampIF(name=f"{self.name}_adc_if", sim=self.sim, samp_clk=self.samp_clk,
                               n_ch=1, blksize=int(self.blksize), n_blk=int(self.n_blk))
        self.adc_if.bind("tx", self.source.rf_ep)
        self.adc_if.bind("rx", self.rfdc.rx_rf)
        self.add_if(self.adc_if)

        # --- the PL domain -----------------------------------------------------------------------
        # No depth overrides on the two that become the DUT's own boundary ports: a top-level AXIS
        # argument cannot carry a FIFO depth (Vitis ignores the pragma, sometimes silently).
        for nm, master, slave in (("adc", self.rfdc.rx_streams[0], self.dut.samp_in),
                                  ("win", self.dut.w_out, self.win_snk.stream_ep)):
            ifc = StreamIF(name=f"{self.name}_{nm}_axis", sim=self.sim, clk=self.axis_clk,
                           bitwidth=w)
            ifc.bind("master", master)
            ifc.bind("slave", slave)
            self.add_if(ifc)
            setattr(self, f"{nm}_axis", ifc)

    @property
    def run_until(self) -> float:
        """Simulated horizon: the metronome's own length plus a two-block tail.

        A testbench constant, not a latency.  The converter is a free-running event source that never
        exhausts — that is what an ADC does — so ``env.run()`` with no bound would never return.
        """
        return (int(self.n_blk) + 2) * self.blk_period

    @property
    def words_per_cycle(self) -> float:
        """How hard the ADC leans on the fabric — ``samp_rate / (samp_per_word * f_axis)``.

        Derived, never declared: the same quantity the XSI converter model is constructed with, so a
        rate changed here cannot leave the two backends running at different speeds.
        """
        return float(self.samp_rate) / (SPW * float(self.axis_freq))


# ---------------------------------------------------------------------------
# Running it, and reading what came out
# ---------------------------------------------------------------------------

def run_pysim(root=None, **kw) -> RfShotRxTB:
    """Build the graph, run it to the metronome's horizon, return the testbench."""
    import tempfile

    tb = RfShotRxTB(name="tb", sim=Simulation(), **kw)
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(root or tmp)
        write_scenario(base, n_blk=int(tb.n_blk))
        tb.source.root = base
        tb.win_snk.root = base
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


def frames_from_sink(tb: RfShotRxTB) -> list[np.ndarray]:
    """The raw frames the sink collected — header **and** samples, as a host would see them."""
    return [np.asarray(b, dtype=np.uint64).ravel() for b in tb.win_snk.words]


def windows_as_codes(frames) -> list[tuple[object, np.ndarray]]:
    """``[(hdr, codes), ...]`` — each window's header and its samples as **signed converter codes**.

    The header is split off by :func:`~waveflow.hw.rf_shot_rx.split_windows`, which is the design's
    own reader of its own layout; the samples are unpacked through the word type's serializer.  Both
    halves go through the one place that owns them, so this function invents nothing.
    """
    from waveflow.hw.rfdc_samp_word import unpack

    out = []
    for hdr, words in split_windows(frames, WORD_BW):
        # The memory holds DENSE words, and `w_out` carries them unchanged -- the re-layout is on the
        # way IN.  So the sample codes come out through the dense element's serializer.
        from waveflow.hw.arrayutils import read_array
        from waveflow.hw.rf_relayout import dense_elem_type

        n = int(words.size) * SPW
        vals = read_array(np.asarray(words, dtype=np.uint64), elem_type=dense_elem_type(WORD),
                          word_bw=WORD_BW, shape=n)
        out.append((hdr, np.asarray(getattr(vals, "val", vals), dtype=np.int64).ravel()))
    return out


def check_windows(frames, *, where: str = "", expect_loss: bool = False) -> np.ndarray:
    """**The gate.**  Every window whole, every header ``CAP_OK``, and the whole run contiguous.

    Returns the concatenated codes, so a caller can go on to check the extent.  With
    *expect_loss* the contiguity claim is inverted — a dirty run that came out contiguous would mean
    the fault injection did nothing.
    """
    wins = windows_as_codes(frames)
    if not wins:
        raise AssertionError(f"{where}no window reached the host.")
    sizes = {int(c.size) for _h, c in wins}
    if sizes != {REGION_SAMPLES}:
        raise AssertionError(
            f"{where}windows of {sorted(sizes)} samples, expected only {REGION_SAMPLES}. A short "
            f"window is the failure a sample count hides: everything in it is correct, there is just "
            f"less of it.")
    flat = np.concatenate([c for _h, c in wins])
    step = np.diff(flat)
    bad = np.flatnonzero(step != 1)
    if expect_loss:
        if not bad.size:
            raise AssertionError(
                f"{where}the stalled reader lost nothing, so this run does not distinguish a design "
                f"that keeps up from one that was never pushed.")
        return flat
    if bad.size:
        i = int(bad[0])
        raise AssertionError(
            f"{where}the windows are not contiguous: sample {i} is {int(flat[i])} and sample "
            f"{i + 1} is {int(flat[i + 1])}, a jump of {int(step[i])}. That gap is capture the "
            f"design lost, and it is invisible in every other reading of this run.")
    bad_hdr = [(i, int(h.status), int(h.n_dropped)) for i, (h, _c) in enumerate(wins)
               if int(h.status) != CAP_OK or int(h.n_dropped)]
    if bad_hdr:
        i, st, n = bad_hdr[0]
        raise AssertionError(
            f"{where}window {i} carries {CAP_STATUS_NAMES.get(st, st)} with n_dropped={n} on a run "
            f"whose samples are contiguous. The header and the data disagree, and a header nobody "
            f"can trust is worse than none.")
    if int(flat[0]) != CODE_BASE:
        raise AssertionError(
            f"{where}the first window starts at code {int(flat[0])}, not {CODE_BASE} — the capture "
            f"handed out a region before it had filled it from the beginning.")
    return flat


def expected_bases(n_windows: int) -> list[int]:
    """The region bases a ping-pong must produce, in order."""
    return [(i % N_REGION) * REGION_WORDS for i in range(int(n_windows))]
