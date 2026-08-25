"""rf_shot_buf.py — the Stage A gate for :class:`~waveflow.hw.rf_shot_buf.RfShotBuf`.

``plans/rf_shot_buf.md`` § *Stage A — the buffer primitive*.  The **module** is framework and lives
in :mod:`waveflow.hw.rf_shot_buf`; what is here is what an example should be — a graph that drives
it, a scenario, and a golden::

    StreamDriver --StreamIF--> RfShotBuf.s_in       s_out --StreamIF--> StreamSink

**No converter, and that is the scope.**  Stage A's job is the buffer primitive: a BRAM, a writer
task, a reader task, and nothing between them.  The RF grid, the converter and any command format
are Stages B and C, and wiring a converter in here would mean this gate could fail for a reason that
is not the buffer's.

**What the gate proves that a word count would not.**  The payload is real converter words — 14-in-16
codes packed four to a 64-bit word — carrying a **ramp**, so a returned word names the index it came
from.  A shot buffer's whole failure mode is returning the wrong words plausibly (a block of the
right shape carrying half a signal, or a shot offset by one), and every one of those passes a
constant check.

**One word per burst**, which is not a detail: a pysim slave dequeues a whole burst per ``get`` and
``nwords_max`` discards the remainder, so a single 256-word burst would be one pysim firing against
256 RTL firings and the two backends would be running different designs.  The XSI ``AxisMaster``
reads the flat ``words.bin`` and never sees the burst bounds, so the stimulus is byte-identical
either way.
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
from waveflow.hw.rf_shot_buf import BUF_DEPTH, SHOT_WORDS, RfShotBuf  # noqa: E402
from waveflow.hw.rfdc_samp_word import Rfsoc4x2SampWord  # noqa: E402
from waveflow.simulation.simulation import Simulation  # noqa: E402
from waveflow.simulation.stream_tb import StreamDriver, StreamSink  # noqa: E402

__all__ = ["BUF_DEPTH", "SHOT_WORDS", "WORD", "NWORD", "DEPTH", "XSI_N_CYCLES", "RfShotBuf",
           "RfShotBufTB", "check_outputs", "check_xsi_outputs", "run_pysim", "shot_words",
           "write_scenario"]

#: **The gated geometry**, and it is the RFSoC 4x2's: four 14-in-16 samples in a 64-bit beat.  The
#: buffer never reads a word arithmetically, so what this fixes is the *geometry* — 64-bit words, 4
#: samples each — and therefore what ``nsamp_held`` and ``nsamp_shot`` mean.
WORD = Rfsoc4x2SampWord.specialize(samp_per_word=4)

#: Depth in **words** (see the module's docstring for why not samples) and words in one shot.  A shot
#: SHORTER than the memory on purpose: a shot that exactly filled the buffer would make an off-by-one
#: in the address arithmetic invisible, because every address would be in range either way.
DEPTH = BUF_DEPTH
NWORD = SHOT_WORDS

#: The ramp's first converter code.  Codes, not slot values: the justification is applied by the
#: packer, which is where it belongs.
CODE_BASE = 1000

#: A fixed run bound for the generated XSI main — a testbench constant, not a latency.  The sink
#: timestamps the real completion, and ``WANT_CYCLES`` in the XSI test is that measurement.
XSI_N_CYCLES = 4000


def shot_codes(nword: int = NWORD, base: int = CODE_BASE) -> np.ndarray:
    """The ramp, in **converter codes** — ``base + i`` at sample ``i``, wrapped into 14 bits signed.

    A ramp rather than a constant because the failure a shot buffer has is *plausible* data: a window
    offset by one word, or the second half of the previous shot.  Both survive a constant check
    without a murmur.
    """
    n = int(nword) * int(WORD.samp_per_word)
    lo = 1 << (int(WORD.bits_per_samp) - 1)
    return ((np.arange(n, dtype=np.int64) + int(base) + lo) % (1 << int(WORD.bits_per_samp))) - lo


def shot_words(nword: int = NWORD, base: int = CODE_BASE) -> np.ndarray:
    """The ramp as packed converter words — what both backends play, one word per beat.

    Through :func:`~waveflow.hw.rfdc_samp_word.pack`, never a hand-rolled shift: the slot order and
    the justification are the word type's to decide, and a second statement of them here is the bug
    that hides at one sample per word.
    """
    from waveflow.hw.rfdc_samp_word import pack

    return np.asarray(pack(WORD, shot_codes(nword, base).reshape(1, -1)), dtype=np.uint64).ravel()


def write_scenario(root, nword: int = NWORD) -> None:
    """Materialize ``<root>/vectors/shot`` — the words both backends drive in."""
    from waveflow.utils.burst_io import write_burst_bundle

    write_burst_bundle([np.array([x], dtype=np.uint64) for x in shot_words(nword)],
                       Path(root) / "vectors" / "shot")


def check_outputs(got, nword: int = NWORD, where: str = "") -> None:
    """The acceptance check, in one place because both backends make the same claim.

    Two named failure modes, because a bare ``!=`` on 256 words tells a reader nothing:

    * **a shifted shot** — every word present but rotated, which is an address that started in the
      wrong place, and is what a missing or early ``rdy`` token looks like;
    * **a short shot** — fewer words than the shot declared, which is the "block of the right shape
      carrying half a signal" this repo keeps meeting and which no counter on the stream can see.
    """
    want = shot_words(nword)
    got = np.asarray(got, dtype=np.uint64).ravel()
    if got.size != want.size:
        raise AssertionError(
            f"{where}rf_shot_buf returned {got.size} words for a {want.size}-word shot. A SHORT "
            f"shot is the failure the response exists to catch at Stage B; at Stage A it means the "
            f"reader stopped early or the loader never finished filling.")
    if np.array_equal(got, want):
        return
    for k in (1, -1, 2, -2):
        if np.array_equal(got, np.roll(want, k)):
            raise AssertionError(
                f"{where}rf_shot_buf returned the shot ROTATED by {k} words — the payload is all "
                f"there and the addressing is off. That is a read that started at the wrong index, "
                f"not a data error, and it is what an early `rdy` token produces.")
    bad = int(np.argmax(got != want))
    raise AssertionError(
        f"{where}rf_shot_buf word {bad}: 0x{int(got[bad]):016x} != 0x{int(want[bad]):016x} "
        f"({int((got != want).sum())} of {want.size} words differ)")


@dataclass
class RfShotBufTB(FreeRunMod):
    """A driver filling the shot, the buffer, and a sink collecting it.

    Nothing else is in the graph, and that absence is the lesson ``plans/rf_shot_buf.md`` § *Stage D*
    wants to teach later: there is **no feedback path anywhere in this diagram**.  Load, play,
    compare — and every mechanism the streaming buffer spends its length on is missing.
    """

    potential_targets: ClassVar[frozenset[str]] = frozenset({SEQUENTIAL_XSI_TB})

    depth: int = DEPTH
    nword: int = NWORD
    n_cycles: int = XSI_N_CYCLES
    axis_freq: float = RFSOC4X2_CLK_HZ
    axis_clk: Clock = field(default_factory=lambda: Clock(freq=RFSOC4X2_CLK_HZ))

    def __post_init__(self) -> None:
        super().__post_init__()
        self.axis_clk = Clock(name=f"{self.name}_axis_clk", freq=float(self.axis_freq))
        w = int(WORD.bitwidth)
        self.dut = RfShotBuf.for_word(WORD, depth=int(self.depth), nword=int(self.nword),
                                      sim=self.sim, name=f"{self.name}_dut", clk=self.axis_clk)
        self.drv = StreamDriver(sim=self.sim, name=f"{self.name}_drv", bitwidth=w,
                                in_bundle="vectors/shot", has_tlast=True)
        self.sink = StreamSink(sim=self.sim, name=f"{self.name}_snk", bitwidth=w,
                               out_bundle="vectors/out", has_tlast=True)
        for c in (self.dut, self.drv, self.sink):
            self.add_comp(c)

        self.in_if = in_if = StreamIF(name=f"{self.name}_in_if", sim=self.sim, clk=self.axis_clk, bitwidth=w)
        in_if.bind(ep_name="master", endpoint=self.drv.stream_ep)
        in_if.bind(ep_name="slave", endpoint=self.dut.s_in)
        self.add_if(in_if)

        self.out_if = out_if = StreamIF(name=f"{self.name}_out_if", sim=self.sim, clk=self.axis_clk, bitwidth=w)
        out_if.bind(ep_name="master", endpoint=self.dut.s_out)
        out_if.bind(ep_name="slave", endpoint=self.sink.stream_ep)
        self.add_if(out_if)


def check_xsi_outputs(xsi_dir, nword: int = NWORD, want_cycles: int | None = None) -> None:
    """Check an XSI run from the bundle it dumped, against the same golden pysim is checked on."""
    from waveflow.utils.burst_io import read_burst_bundle

    vdir = Path(xsi_dir) / "vectors"
    assert (vdir / "out").is_dir(), f"no capture bundle at {vdir / 'out'} — the run did not dump one"
    got = np.concatenate(read_burst_bundle(vdir / "out"))
    check_outputs(got, nword, where="XSI: ")
    if want_cycles is not None:
        cycles = np.fromfile(vdir / "out" / "cycles.bin", dtype="<u8")
        last = int(cycles[-1])
        assert last == want_cycles, (
            f"rf_shot_buf completed at cycle {last}, gate expects {want_cycles}. That is a real "
            f"behaviour change: either a regression or an improvement worth re-recording.")


def run_pysim(root=None, nword: int = NWORD) -> RfShotBufTB:
    """Run the graph in SimPy and return the testbench — the toolchain-free golden.

    Returns the TB rather than the words so a caller can also read the **phase counters**, which is
    the half of this design a byte comparison cannot see.
    """
    import tempfile

    tb = RfShotBufTB(name="tb", sim=Simulation(), nword=int(nword))
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(root or tmp)
        write_scenario(root, nword)
        tb.drv.root = root
        tb.sink.root = root
        tb.sim.run_sim()
    return tb


def captured_words(tb: RfShotBufTB) -> np.ndarray:
    """What the sink collected."""
    return np.concatenate(tb.sink.words) if tb.sink.words else np.zeros(0, dtype=np.uint64)
