"""rf_blk_delay.py — **the pattern-B example**: ``Rfdc → RfSampBuf(RX) → BlkDelay → RfSampBuf(TX) → Rfdc``.

``plans/adc_model.md`` § *Two design patterns* specifies this, and it is the pattern the plan makes
the **default**: a user's logic reaches the converter through a sample buffer at each end rather than
by touching the boundary port itself.  ``rf_loopback`` is deliberately left alone as the pattern-A
case study — its 72 dropped words and its silently-ignored depth pragma are the evidence for B
existing, and deleting the evidence would delete the argument.

::

    RfDataSource --RFSampIF--> Rfdc.rx_rf | rx_stream --> [RfSampBufRx] --s_out--+
                                                               ^ s_cmd            |
                                                               |                  v
                                                          [BlkDelay] <------------+
                                                               | TxCmd + payload
                                                               v
    RfDataSink   <--RFSampIF-- Rfdc.tx_rf | tx_stream <-- [RfSampBufTx] <---------+

**Why a delay and not a pass-through.**  A pass-through is a wire: it would prove the plumbing
carries bytes and nothing about the buffers.  A *delay* is the minimal block that makes the
timestamp mean something — ``out_ts = in_ts + delay`` is exactly ``RfSampBuf``'s contract, and here
it is exercised rather than described: :class:`BlkDelay` reads block *k* out of the RX buffer with an
``RxCmd`` naming sample index ``k·blksize`` and writes it into the TX buffer with a ``TxCmd`` naming
``(k + delay_blocks)·blksize``.  The delay is *observable*: the sink's samples appear that many
blocks later on the DAC's grid, and the test measures the shift rather than asserting it.

**The delay is in BLOCKS, not samples**, and the reason is a contract rather than convenience.
``RfSampBufLoader`` refuses a window that is not a whole number of words
(:data:`~waveflow.hw.rf_samp_buf.RF_SAMP_BUF_MISALIGNED`) — sub-word windows would mean unpacking,
selecting and re-packing inside a loop that must stay cheap, which that module deliberately does not
do.  A block is a multiple of ``samp_per_word`` by construction, so a block-granular delay is
word-aligned for free and the TX side never needs a non-block-aligned read.  A sample-granular delay
would put that difficulty back, on the module that most needs to stay simple.

**Two buffers, not one shared**, per the plan — and the code agrees beyond it: the two progress
channels point in opposite directions, and a ``T2pBram`` has exactly one write port and one read
port, which RX and TX would then have to arbitrate for.  Sharing would add a contention question to
two modules whose whole purpose is to have none.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

import numpy as np

HERE = Path(__file__).resolve().parent

from waveflow.build.composite_gen import RFSOC4X2_CLK_HZ  # noqa: E402
from waveflow.hw.clock import Clock  # noqa: E402
from waveflow.hw.codegen_targets import COMPOSITE_KERNEL, SEQUENTIAL_XSI_TB  # noqa: E402
from waveflow.hw.hw_freerun import FreeRunMod  # noqa: E402
from waveflow.hw.hw_module import HwParam  # noqa: E402
from waveflow.hw.interface import StreamIF, StreamIFMaster, StreamIFSlave  # noqa: E402
from waveflow.hw.mem_stream import KernelTask  # noqa: E402
from waveflow.hw.rf_samp_buf import (  # noqa: E402
    IDX_BW,
    RF_SAMP_BUF_OK,
    RfSampBufRx,
    RxCmd,
    RxResp,
)
from waveflow.hw.rf_samp_buf_tx import RfSampBufTx, TxCmd, TxResp  # noqa: E402
from waveflow.hw.rf_sample_if import RFSampIF  # noqa: E402
from waveflow.simulation.rf_tb import RfDataSink, RfDataSource  # noqa: E402
from waveflow.simulation.simobj import ProcessGen  # noqa: E402
from waveflow.simulation.simulation import Simulation  # noqa: E402
from waveflow.simulation.stream_tb import StreamSink  # noqa: E402

from examples.rf_loopback.rfdc import Rfdc  # noqa: E402
from waveflow.hw.rfdc_samp_word import RfdcSampWord, Rfsoc4x2SampWord  # noqa: E402

# ---------------------------------------------------------------------------
# Geometry — the RFSoC 4x2, and every number here is forced by something
# ---------------------------------------------------------------------------

#: Sample width in bits.  **The AXIS container, not the converter's resolution.**  The ZU48DR's
#: converters are 14-bit (RF-ADC) and 14-bit (RF-DAC); AMD's RFDC IP presents them on a 16-bit
#: sample slot, sign-extended, so the fabric always sees 16.  PG269 documents the 16-bit slot; what
#: is *not* verified here is the exact padding/justification the IP applies, because nothing in this
#: repo talks to the real IP yet — see the PR's "could not verify".  16 is the container width, which
#: is what the packing arithmetic needs, and it is right whatever the low bits turn out to mean.
SAMP_BW = 16

#: Samples per AXIS word.  4 x 16 = 64 bits, **exactly** ``Rfdc``'s ceiling — the widest word this
#: repo's ``uint64`` burst-bundle word can carry, and the reason a real 1 GSPS RFDC (128 bits or
#: wider) cannot be modelled yet.
SAMP_PER_WORD = 4

#: Samples per RF block — one SimPy event, and one ``RxCmd``/``TxCmd`` window.
BLKSIZE = 256

#: RF blocks the source plays.
N_BLK = 12

#: **The delay, in blocks — and 4 is the MEASURED floor at this sample rate, not a guess.**
#:
#: Swept 2..6 in pysim at 250 MSa/s: at 2 and 3 every one of the 12 ``TxCmd``\ s comes back
#: :data:`~waveflow.hw.rf_samp_buf.RF_SAMP_BUF_TOO_LATE` with *partial* placement, because the loader
#: keeps draining the frame after the verdict; at 4 and above all 12 place their full 256.
#: ``test_rf_blk_delay.py`` re-runs that sweep, so the floor is a checked property, not a comment.
#:
#: **The floor scales with the sample rate**, which matters if this example's rate is ever raised: it
#: is the loop's round trip expressed in *block periods*, and a block period is ``blksize /
#: samp_rate``.  Measured at 400 MSa/s the floor is 6, not 4 — a delay carried across a rate change
#: is a delay measured for a different design.
#:
#: Why a floor exists at all: the ADC and the DAC share one grid, so block *k* is only complete at
#: the instant the DAC's period *k* comes due; the RX capture cannot serve it before then, the round
#: trip through two buffers costs more periods, and the TX loader refuses a slot the player has
#: reached.  The arithmetic bound is 2 (one period for the ADC to finish the block, one for the
#: capture to serve it); the rest is the fabric round trip, and that is why the floor has to be
#: measured rather than derived.
DELAY_BLOCKS = 4

#: The minimum this example is known to work at — asserted, so a change that raises the round-trip
#: cost fails here rather than silently needing a bigger delay.
MIN_DELAY_BLOCKS = 4

#: Sample rate.  **250 MSPS — and it was NOT raised, which is a result rather than an omission.**
#:
#: The loop's ceiling is the slowest of its four stages, because every sample crosses all four.
#: Measured per-word costs (achieved ``PipelineII``; ``tests/examples/test_rf_samp_buf_fire_cycles.py``):
#:
#:   ==================  ==============  =====================================
#:   stage               cycles/word     ceiling at spw=4, 250 MHz
#:   ==================  ==============  =====================================
#:   RX ingress          1               1000 MSPS
#:   **RX capture**      **2**           **500 MSPS  <- binds**
#:   **TX loader**       **2**           **500 MSPS  <- binds**
#:   TX player           1               1000 MSPS
#:   ==================  ==============  =====================================
#:
#: so the loop ceiling is **500 MSPS**.  Pipelining the two converter-facing bodies to II=1 on
#: 2026-08-18 moved neither half's ceiling, because the two stages that bind are the other two — and
#: neither can currently be fixed: the capture's per-word wait asks whether *this* word has been
#: written yet (hoisting it would delete the straddling case it exists to serve), and the loader's
#: *can* be hoisted, and csynth then reports II=1, but the RTL is wrong — see
#: :attr:`~waveflow.hw.rf_samp_buf_tx.RfSampBufLoader.word_cycles`.
#:
#: **400 MSa/s was tried and is not shipped.**  It is inside the 500 ceiling, ``check_rate`` accepts
#: it, and pysim is clean end to end at ``delay_blocks = 6`` (the floor re-measured at that rate).
#: At RTL it is not: the played ramp starts 1028 samples early instead of the expected few and breaks
#: after about seven blocks.  That divergence was identified, not resolved, so the rate stays where
#: both backends are verified.  Raising it is worth doing *after* one of the two binding stages is,
#: since the ceiling does not move until then anyway.
SAMP_RATE = 250e6

#: RX buffer depth in **words**.  1024 words x 4 samples = 4096 samples = 16 blocks of history.
RX_DEPTH = 1024
#: TX buffer depth in words — 2048 x 4 = 8192 samples, so the loader can run well ahead of the player.
TX_DEPTH = 2048

#: Sample values are a ramp, so every played sample identifies the slot it came from.  A constant
#: would pass a loop that delivered the right *number* of samples from the wrong place, which is the
#: whole failure mode a pair of circular buffers has.
SAMP_BASE = 1000

#: Blocks the **source** plays — one more than the design relays, and the extra one is not padding.
#:
#: **The last block of a capture cannot complete unless the converter keeps running**, and that is a
#: property of the progress channel rather than a bug.  The RX ingress reports its write pointer with
#: a *non-blocking* write to a depth-1 channel: updates are dropped rather than allowed to stall the
#: converter, which is correct, because a stalled ingress loses samples and a dropped position report
#: costs nothing — **while more are coming**.  When the ADC stops, no more are coming, so a final
#: report lost to a full channel is never repaired and the capture waits forever for a window it can
#: no longer prove was written.
#:
#: Measured at RTL: with the source playing exactly ``N_BLK`` blocks, 11 of the 12 commands came back
#: and the twelfth hung — the ADC's last progress update had been dropped.  A real converter does not
#: stop, so the honest fix is to make the scenario resemble one rather than to weaken the buffer.
#: pysim does not show this at all, because its channel carries the last value regardless — see the
#: fidelity note in the PR.
SRC_NBLK = N_BLK + 1


def ramp_samples(nsamp: int = SRC_NBLK * BLKSIZE, base: int = SAMP_BASE) -> np.ndarray:
    """The sample stream the source plays: ``base + i`` at sample index ``i``."""
    return ((np.arange(int(nsamp), dtype=np.int64) + int(base)) % (1 << SAMP_BW)).astype(np.uint64)


# ---------------------------------------------------------------------------
# The user's logic
# ---------------------------------------------------------------------------

@dataclass
class BlkDelay(FreeRunMod):
    """Move block *k* from the RX buffer to slot *k + delay* of the TX buffer.

    One firing is one block, and the whole of pattern B's point is what this body does **not** have
    to contain: no never-stall obligation, no depth argument, no rate contract.  It reads a window by
    *sample index* and writes one by *sample index*, and the buffers either side own the converter
    boundaries.  Compare ``rf_loopback``'s ``RfSampPassThrough``, which had to be split into two
    tasks over a sized FIFO and still needed a hand-written word-granular body to stop dropping
    samples — that difficulty is what moved into ``RfSampBuf``, once, so this module never meets it.

    It may block freely on all three of its streams.  Nothing downstream misses a deadline while it
    waits: the TX player keeps playing whatever this is doing, and the RX ingress keeps filling.
    """

    cpp_kernel_name: ClassVar[str | None] = "rf_blk_delay"

    #: **Fabric cycles the relay loop costs per word** — the achieved II of ``VITIS_LOOP_74_1``.
    #:
    #: MEASURED, from ``blk_delay_task_64_4_256_4_16_Pipeline_VITIS_LOOP_74_1_csynth.xml``: 64
    #: iterations at ``Interval = 64``, so II = 1.  A stream-to-stream copy with no memory in the path
    #: is the one shape that reaches II=1 easily — which is why this module is the *cheapest* thing in
    #: the loop rather than its bottleneck, and why pattern B's user block does not have to think
    #: about rate at all.
    word_cycles: ClassVar[int] = 1

    #: **Fixed cycles per firing**, on top of :attr:`word_cycles` per word — the two commands and the
    #: loop's prologue.
    #:
    #: MEASURED: ``blk_delay_task_64_4_256_4_16_s`` reports ``Worst-caseLatency = 67`` for a firing
    #: that relays 64 words, and ``67 - 64 x 1 = 3``.  It is charged in :meth:`run_iter` because a twin
    #: that relays a burst and pays nothing for it is rate-blind — the defect PR #160 removed from the
    #: RX ingress — even though this body has margin to spare.
    #:
    #: **Not** called ``fire_cycles``: that name means cycles per firing of a body that handles ONE
    #: word, and both ``RfSampBuf`` bodies that declare it do exactly that.  This firing is a block, so
    #: giving it the same name with a different meaning is how a number gets carried into a rate
    #: formula it does not belong in.
    fire_overhead: ClassVar[int] = 3

    #: AXIS word width in bits — ``samp_per_word * SAMP_BW``.
    bitwidth: HwParam[int] = SAMP_BW * SAMP_PER_WORD
    samp_per_word: HwParam[int] = SAMP_PER_WORD
    #: Samples in one block — the window each command names.
    blksize: HwParam[int] = BLKSIZE
    #: The delay, in **blocks**.  See the module docstring for why blocks and not samples.
    delay_blocks: HwParam[int] = DELAY_BLOCKS
    #: Blocks to relay before going idle — a **testbench bound, not a design one**, and therefore a
    #: plain field rather than an ``HwParam``: it must not reach the elaborated signature or the
    #: generated top.  The synthesized body has no such bound and does not need one; it idles by
    #: blocking on ``s_in`` for a block the ADC has not produced, which is what an ``hls::task`` with
    #: nothing to do does anyway.  pysim needs the count only because a free-running generator that
    #: keeps scheduling wake-ups never lets ``env.run()`` return.
    n_blk: int = N_BLK
    clk: Clock = field(default_factory=lambda: Clock(freq=RFSOC4X2_CLK_HZ))

    def __post_init__(self) -> None:
        super().__post_init__()
        w = int(self.bitwidth)
        if int(self.blksize) % int(self.samp_per_word):
            raise ValueError(
                f"blksize {int(self.blksize)} is not a whole number of "
                f"{int(self.samp_per_word)}-sample words, so a block-granular delay could not be "
                f"word-aligned and RfSampBufLoader would refuse every command")
        if int(self.delay_blocks) < 2:
            raise ValueError(
                f"delay_blocks must be at least 2, got {int(self.delay_blocks)}. The ADC and the DAC "
                f"share one grid, so block k is only complete when the DAC's period k comes due; a "
                f"delay of 1 asks the TX buffer to place samples in a slot the player is already at. "
                f"2 is the arithmetic floor; the MEASURED floor for this graph is "
                f"{MIN_DELAY_BLOCKS} (see DELAY_BLOCKS), because the round trip through two buffers "
                f"costs periods the arithmetic does not count.")
        #: Commands out to the RX capture.
        self.s_cmd = StreamIFMaster(sim=self.sim, name=f"{self.name}_s_cmd", bitwidth=w,
                                    has_tlast=True)
        #: Captured samples in from the RX buffer.
        self.s_in = StreamIFSlave(sim=self.sim, name=f"{self.name}_s_in", bitwidth=w, has_tlast=True)
        #: ``TxCmd`` then its payload, in band, out to the TX loader.
        self.s_out = StreamIFMaster(sim=self.sim, name=f"{self.name}_s_out", bitwidth=w,
                                    has_tlast=True)
        for ep in (self.s_cmd, self.s_in, self.s_out):
            self.add_endpoint(ep)
        #: The block this module will relay next — the only state it keeps.
        self.k = 0
        #: Blocks relayed end to end.
        self.n_relayed = 0

    @property
    def nwords_blk(self) -> int:
        """Words in one block — ``blksize / samp_per_word``."""
        return int(self.blksize) // int(self.samp_per_word)

    @property
    def delay_samples(self) -> int:
        """The delay in sample indices, which is what a command actually carries."""
        return int(self.delay_blocks) * int(self.blksize)

    def run_iter(self) -> ProcessGen[None]:
        """One block: ask for it by sample index, take it, place it ``delay`` blocks later.

        ``out_ts = in_ts + delay`` is the whole body, and it is one line of arithmetic on the two
        commands' ``start`` fields — which is the point.  Everything that makes a converter hard
        lives in the two buffers.
        """
        k = int(self.k)
        if k >= int(self.n_blk):
            # Nothing more to relay.  Park on an event that is never triggered rather than looping
            # on a timeout: a free-running body that keeps scheduling wake-ups keeps SimPy's queue
            # non-empty, and `env.run()` would never return -- the run would hang rather than end.
            # Blocking forever is what an idle hls::task does anyway (it waits on an empty stream),
            # so this is also the more faithful idle.
            yield self.sim.env.event()
            return

        in_ts = k * int(self.blksize)
        out_ts = in_ts + self.delay_samples

        # 1. Ask the RX buffer for block k, by SAMPLE INDEX -- not a buffer address.  The capture
        #    blocks per word until the ADC has produced them, so this paces itself.
        yield from self.s_cmd.write(RxCmd(tid=k + 1, start=in_ts, nsamp=int(self.blksize)))

        # 2. Place it delay_blocks later.  The command goes FIRST -- before a single sample has
        #    arrived -- and the payload follows it in band, which is the framing RfSampBufLoader
        #    reads.  Committing to the destination up front is what lets the block be RELAYED word by
        #    word below rather than buffered here: this module holds no block storage at all, and the
        #    generated body holds none either.  Buffering the block first would work equally well in
        #    pysim and would quietly imply a BLKSIZE-word buffer that the hardware does not have.
        yield from self.s_out.write(TxCmd(tid=k + 1, start=out_ts, nsamp=int(self.blksize)))

        left = self.nwords_blk
        while left:
            got = np.asarray((yield from self.s_in.get())).ravel()
            if got.size > left:
                raise RuntimeError(
                    f"{type(self).__name__} '{self.name}': a burst of {got.size} words overran the "
                    f"{left} still owed on this block; a burst that straddles two blocks would "
                    f"misalign every command after it.")
            yield from self.s_out.write(got.astype(np.uint64))
            left -= int(got.size)

        # Charge the firing what csynth says it costs.  Not because this body is anywhere near
        # binding -- it is the cheapest task in the loop -- but because a twin that relays a burst
        # and pays nothing for it cannot report a rate at all, and a module whose cost is invisible
        # is one nobody notices getting slower.
        yield self.timeout((self.nwords_blk * self.word_cycles + self.fire_overhead)
                           * self.clk.period)

        self.k = k + 1
        self.count_relayed()

    def count_relayed(self) -> None:
        self.n_relayed += 1

    def kernel_task(self) -> KernelTask:
        """A **hand-written** ``hls::task`` body, for the reason the ``RfSampBuf`` bodies are.

        The relay loop reads one stream and writes another under a running static index; the
        extractor's vocabulary does not cover it, and a leaf whose ``kernel_task()`` names a header is
        never extracted.  ``n_blk`` is absent from the template arguments on purpose — see the field.
        """
        return KernelTask("blk_delay_task", "blk_delay_task.h",
                          ("s_cmd", "s_in", "s_out"),
                          template_args=(int(self.bitwidth), int(self.samp_per_word),
                                         int(self.blksize), int(self.delay_blocks), IDX_BW))


# ---------------------------------------------------------------------------
# The design: three modules, one generated top
# ---------------------------------------------------------------------------

@dataclass
class RfBlkDelayLoop(FreeRunMod):
    """``RfSampBufRx → BlkDelay → RfSampBufTx`` as **one** synthesis scope.

    This is the whole of pattern B as a user would assemble it, and the interesting thing about the
    class is how little it says: three ``add_comp``\\ s, three ``add_if``\\ s, and a list of boundary
    names.  The buffers arrive as finished modules — their memories, their progress channels and
    their never-stall obligations come with them — so what is written here is only the *loop*.

    **It composes composites, and that is new.**  ``RfSampBufRx`` and ``RfSampBufTx`` are themselves
    composites of two tasks and a BRAM, so this top is two levels deep, while ``hls::task`` has no
    hierarchy at all: the generated kernel is a flat list of **five** tasks
    (ingress, capture, blk_delay, loader, player) joined by **six** channels, with two memories beside
    it in the wrapper.  :func:`~waveflow.build.composite_gen.kernel_tasks` does that flattening; this
    class does not know about it, which is the point — a design should be able to reuse a module
    without knowing whether it is a leaf.

    The AXIS boundary is two ports: samples in from the ADC and samples out to the DAC.  Everything
    else crossing it is a *response* stream, which the testbench collects and the gate checks.
    """

    cpp_kernel_name: ClassVar[str | None] = "rf_blk_delay"
    potential_targets: ClassVar[frozenset[str]] = frozenset({COMPOSITE_KERNEL})

    bitwidth: HwParam[int] = SAMP_BW * SAMP_PER_WORD
    samp_per_word: HwParam[int] = SAMP_PER_WORD
    blksize: HwParam[int] = BLKSIZE
    delay_blocks: HwParam[int] = DELAY_BLOCKS
    rx_depth: HwParam[int] = RX_DEPTH
    tx_depth: HwParam[int] = TX_DEPTH
    horizon_margin: HwParam[int] = 4 * SAMP_PER_WORD
    #: Modelling shapes, not hardware parameters — plain fields for the reason
    #: :attr:`~waveflow.hw.rf_samp_buf_tx.RfSampBufTx.blk_words` is one.
    n_blk: int = N_BLK
    dac_word_rate: float | None = None
    clk: Clock = field(default_factory=lambda: Clock(freq=RFSOC4X2_CLK_HZ))

    def __post_init__(self) -> None:
        super().__post_init__()
        w, spw = int(self.bitwidth), int(self.samp_per_word)

        self.rx = RfSampBufRx(name=f"{self.name}_rx", sim=self.sim, bitwidth=w, samp_per_word=spw,
                              depth=int(self.rx_depth),
                              horizon_margin=int(self.horizon_margin), clk=self.clk)
        self.dut = BlkDelay(name=f"{self.name}_dly", sim=self.sim, bitwidth=w, samp_per_word=spw,
                            blksize=int(self.blksize), delay_blocks=int(self.delay_blocks),
                            n_blk=int(self.n_blk), clk=self.clk)
        self.tx = RfSampBufTx(name=f"{self.name}_tx", sim=self.sim, bitwidth=w, samp_per_word=spw,
                              depth=int(self.tx_depth),
                              horizon_margin=int(self.horizon_margin),
                              blk_words=int(self.blksize) // spw,
                              dac_word_rate=self.dac_word_rate, clk=self.clk)
        for c in (self.rx, self.dut, self.tx):
            self.add_comp(c)

        # The three channels that ARE the loop.  Depth 2 on the command paths (one command in flight
        # is all this body ever has) and a whole block on the sample paths, because the capture
        # streams a block through while `blk_delay` is still forwarding the previous one -- an
        # undersized channel here would serialise the two and show up as TOO_LATE, not as a stall.
        nwords = int(self.blksize) // spw
        self._wire("cmd_if", self.dut.s_cmd, self.rx.s_cmd, depth=2)
        self._wire("samp_if", self.rx.s_out, self.dut.s_in, depth=nwords)
        self._wire("load_if", self.dut.s_out, self.tx.s_in, depth=nwords)

        #: ``kernel_tasks`` x ``add_endpoint`` order, with every endpoint bound above removed.  The
        #: four ``*_buf_*`` entries are ports of the KERNEL, joined to the two memories in the
        #: wrapper — one memory per buffer, never shared (see the module docstring).
        self.boundary = ["s_in", "rx_buf_w", "rx_resp", "rx_buf_r",
                         "tx_buf_w", "tx_resp", "tx_buf_r", "s_out"]

        # Convenience refs — the boundary endpoints live on the grandchildren.
        self.s_in = self.rx.s_in
        self.s_out = self.tx.s_out
        self.rx_resp = self.rx.s_resp
        self.tx_resp = self.tx.s_resp

    def _wire(self, nm: str, master, slave, depth: int) -> StreamIF:
        i = StreamIF(name=f"{self.name}_{nm}", sim=self.sim, clk=self.clk,
                     bitwidth=int(self.bitwidth), depth=depth)
        i.bind(ep_name="master", endpoint=master)
        i.bind(ep_name="slave", endpoint=slave)
        self.add_if(i)
        return i

    def check_rates(self, samp_rate: float, f_axis: float) -> tuple[float, float]:
        """Refuse a converter either half cannot sustain, and return both utilisations.

        **Both are checked and neither is bypassed.**  A loop contains an ingress *and* a player and
        they do not cost the same — ``fire_cycles`` is 2 and 3, measured — so the loop's ceiling is
        the TX half's, ``samp_per_word * f_axis / 3``.  A rate picked from the RX half alone is
        refused here rather than left to underrun the DAC with no protocol event to mark it.
        """
        return (self.rx.check_rate(float(samp_rate), float(f_axis)),
                self.tx.check_rate(float(samp_rate), float(f_axis)))


# ---------------------------------------------------------------------------
# The testbench graph
# ---------------------------------------------------------------------------

@dataclass
class RfBlkDelayTB(FreeRunMod):
    """The whole pattern-B loop, with a real converter at both ends.

    One ``Rfdc`` carrying both directions, because that is what a tile is: the ADC and DAC sample
    counters hold a fixed relation, and the delay this example measures is a relation between them.
    """

    potential_targets: ClassVar[frozenset[str]] = frozenset({SEQUENTIAL_XSI_TB})

    n_blk: int = N_BLK
    blksize: int = BLKSIZE
    samp_rate: float = SAMP_RATE
    axis_freq: float = RFSOC4X2_CLK_HZ
    #: **The converter's packing convention**, as one type — samples per beat, effective bits,
    #: container bits, and the two rules a serializer cannot know.  Replaces the ``nbits`` /
    #: ``samp_per_word`` pair, one of which meant two things.  Everything downstream (the DUT's word
    #: width, the block-to-word arithmetic) is read off it, never restated.
    word: type[RfdcSampWord] = Rfsoc4x2SampWord.specialize(samp_per_word=SAMP_PER_WORD)
    delay_blocks: int = DELAY_BLOCKS
    rx_depth: int = RX_DEPTH
    tx_depth: int = TX_DEPTH
    #: Samples of margin the TX loader gives up to bound the play channel's staleness.
    #:
    #: **Measured for this example rather than inherited**, which matters because it is the last
    #: constant in ``RfSampBuf`` that was carried from a design where it bounded a *different* test,
    #: and two other inherited constants in that module have already been measured wrong.
    #:
    #: Instrumented over a run: the loader's view of the play pointer lagged the true one by at most
    #: **8 samples** (two words), mean 4.3, across its firings.  ``4 * samp_per_word`` is twice that
    #: worst case, and it is written as a formula rather than a bare number so it scales with the
    #: geometry instead of being carried again.  At this example's ``samp_per_word = 4`` it comes to
    #: 16 — the same value RX uses, now for a reason that belongs to this design.
    horizon_margin: int = 4 * SAMP_PER_WORD
    #: Fixed run bound for the generated XSI main.
    n_cycles: int = 60000
    axis_clk: Clock = field(default_factory=lambda: Clock(freq=RFSOC4X2_CLK_HZ))

    def check_rates(self) -> tuple[float, float]:
        """The design's own rate check, run on the design — see :meth:`RfBlkDelayLoop.check_rates`."""
        return self.loop.check_rates(float(self.samp_rate), float(self.axis_freq))

    def __post_init__(self) -> None:
        super().__post_init__()
        self.axis_clk = Clock(name=f"{self.name}_axis_clk", freq=float(self.axis_freq))
        self.samp_clk = Clock(name=f"{self.name}_samp_clk", freq=float(self.samp_rate))
        self.blk_period = int(self.blksize) / float(self.samp_rate)

        self.rfdc = Rfdc(name=f"{self.name}_rfdc", sim=self.sim, n_rx=1, n_tx=1,
                         word=self.word)
        w = self.rfdc.axis_bitwidth
        spw = int(self.word.samp_per_word)

        #: **The design under test, as one module.**  The testbench instantiates the same class the
        #: synthesis flow elaborates, so the pysim golden and the RTL gate cannot be running different
        #: graphs — which is the trap a testbench that re-wires the three parts itself walks into.
        self.loop = RfBlkDelayLoop(name=f"{self.name}_loop", sim=self.sim, bitwidth=w,
                                   samp_per_word=spw, blksize=int(self.blksize),
                                   delay_blocks=int(self.delay_blocks), rx_depth=int(self.rx_depth),
                                   tx_depth=int(self.tx_depth),
                                   horizon_margin=int(self.horizon_margin), n_blk=int(self.n_blk),
                                   dac_word_rate=float(self.samp_rate) / spw, clk=self.axis_clk)
        # Convenience refs — every test and every counter reads through these.
        self.rx, self.tx, self.dut = self.loop.rx, self.loop.tx, self.loop.dut
        #: Both utilisations — checked, not assumed.
        self.rx_util, self.tx_util = self.check_rates()

        self.source = RfDataSource(name=f"{self.name}_src", sim=self.sim, in_bundle="vectors/rf_in")
        self.sink = RfDataSink(name=f"{self.name}_sink", sim=self.sim, out_bundle="vectors/rf_out")
        self.rxresp_sink = StreamSink(sim=self.sim, name=f"{self.name}_rxresp", bitwidth=w,
                                      out_bundle="vectors/rxresp", has_tlast=True)
        self.txresp_sink = StreamSink(sim=self.sim, name=f"{self.name}_txresp", bitwidth=w,
                                      out_bundle="vectors/txresp", has_tlast=True)
        for c in (self.loop, self.rfdc, self.source, self.sink,
                  self.rxresp_sink, self.txresp_sink):
            self.add_comp(c)

        # --- the RF domain: one tile, two grids on one epoch -----------------------------------
        # The ADC grid plays SRC_NBLK blocks, one more than the design relays — see SRC_NBLK for why
        # the last relayed block cannot complete without it.
        self.adc_if = RFSampIF(name=f"{self.name}_adc_if", sim=self.sim, samp_clk=self.samp_clk,
                               n_ch=1, blksize=int(self.blksize), n_blk=int(self.n_blk) + 1)
        self.adc_if.bind("tx", self.source.rf_ep)
        self.adc_if.bind("rx", self.rfdc.rx_rf)
        self.add_if(self.adc_if)

        # The DAC grid runs longer than the source plays: the delay pushes the last block out by
        # `delay_blocks` periods, and a grid that stopped with the source would truncate it.
        self.dac_if = RFSampIF(name=f"{self.name}_dac_if", sim=self.sim, samp_clk=self.samp_clk,
                               n_ch=1, blksize=int(self.blksize),
                               n_blk=int(self.n_blk) + int(self.delay_blocks) + 1)
        self.dac_if.bind("tx", self.rfdc.tx_rf)
        self.dac_if.bind("rx", self.sink.rf_ep)
        self.add_if(self.dac_if)

        # --- the PL domain ---------------------------------------------------------------------
        def wire(nm, master, slave):
            i = StreamIF(name=f"{self.name}_{nm}", sim=self.sim, clk=self.axis_clk, bitwidth=w)
            i.bind("master", master)
            i.bind("slave", slave)
            self.add_if(i)
            return i

        # Only the DESIGN's boundary is wired here — the three channels inside the loop belong to the
        # loop and are wired by it.  What crosses this line is what would cross it on the board.
        self.adc_axis = wire("adc_axis", self.rfdc.rx_stream, self.loop.s_in)
        wire("rxresp_axis", self.loop.rx_resp, self.rxresp_sink.stream_ep)
        wire("txresp_axis", self.loop.tx_resp, self.txresp_sink.stream_ep)
        self.dac_axis = wire("dac_axis", self.loop.s_out, self.rfdc.tx_stream)


def write_scenario(root) -> None:
    """Materialize ``<root>/vectors/rf_in`` — what BOTH backends play."""
    from waveflow.simulation.rf_tb import write_rf_bundle

    ramp = ramp_samples()
    full = float(1 << (SAMP_BW - 1))
    blocks = [np.asarray(_signed(ramp[i * BLKSIZE:(i + 1) * BLKSIZE]), dtype=float).reshape(1, -1)
              / full for i in range(SRC_NBLK)]
    write_rf_bundle(blocks, Path(root) / "vectors" / "rf_in")


def _signed(words: np.ndarray) -> np.ndarray:
    w = np.asarray(words, dtype=np.int64)
    return np.where(w >= (1 << (SAMP_BW - 1)), w - (1 << SAMP_BW), w)


def run_pysim(root=None, tb=None):
    """Run the loop in SimPy and return the testbench."""
    import tempfile

    tb = tb or RfBlkDelayTB(name="tb", sim=Simulation())
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(root or tmp)
        write_scenario(base)
        for part in (tb.source, tb.sink, tb.rxresp_sink, tb.txresp_sink):
            part.root = base
        tb.sim.run_sim()
    return tb


def played_samples(tb) -> np.ndarray:
    """What the DAC actually played, as unsigned samples — the far side of the converter."""
    if not tb.sink.blocks:
        return np.zeros(0, dtype=np.uint64)
    flat = np.concatenate([np.asarray(b, dtype=np.float64).ravel() for b in tb.sink.blocks])
    ints = np.rint(flat * float(1 << (SAMP_BW - 1))).astype(np.int64)
    return (ints % (1 << SAMP_BW)).astype(np.uint64)


def _resps(sink, cls, w):
    """Deserialize responses **through the schema**, never by slicing words.

    At a 64-bit AXIS word all three 16-bit fields ride in one word, so a raw slice returns the
    packed integer rather than the fields — which is exactly the class of bug the generated
    serializers exist to prevent, and it is invisible at a 16-bit word where one field is one word.
    """
    if not sink.words:
        return []
    flat = np.concatenate(sink.words).astype(np.uint64)
    n = cls.nwords_per_inst(w)
    out = []
    for i in range(0, flat.size, n):
        r = cls().deserialize(flat[i:i + n], word_bw=w)
        out.append(tuple(int(getattr(r, f)) for f in cls.elements))
    return out


def rx_responses(tb):
    """``(tid, status, nsent)`` from the RX capture."""
    return _resps(tb.rxresp_sink, RxResp, int(tb.rfdc.axis_bitwidth))


def tx_responses(tb):
    """``(tid, status, nloaded)`` from the TX loader."""
    return _resps(tb.txresp_sink, TxResp, int(tb.rfdc.axis_bitwidth))


def measured_delay(tb):
    """``out_ts - in_ts``, **measured from the played samples** rather than asserted.

    The source ramp makes each sample name its own input index, so finding where input sample 0
    lands on the DAC grid gives the shift directly.  ``None`` if the first block never played.
    """
    played = played_samples(tb)
    ramp = ramp_samples()
    if played.size < 16:
        return None
    want = ramp[:16]
    for i in range(played.size - 16):
        if np.array_equal(played[i:i + 16], want):
            return i
    return None
