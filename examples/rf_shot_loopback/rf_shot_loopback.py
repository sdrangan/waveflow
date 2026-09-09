"""rf_shot_loopback.py — ``plans/rf_shot_absolute.md`` S3: **the delay is an address difference**.

One converter, both directions, and a path between them::

    StreamDriver --[ShotTxHdr | samples]--> RfShotTx.s_in
    RfShotTx.samp_out --> Rfdc.tx_streams[0] | Rfdc.tx_rf --RFSampIF--> RfSampDelay
                                                                            |
    StreamSink <-- RfShotRx.w_out <-- Rfdc.rx_streams[0] | Rfdc.rx_rf <--RFSampIF--+

Both buffers are built with ``absolute_index = 1``, so **a memory index is a timestamp**: the
transmitter writes sample *j* of its waveform at ``mem[j mod depth]`` and plays it there, and the
receiver captures whatever arrives at ``mem[k mod depth]`` where *k* counts every sample the
converter has handed it since reset.

**Therefore a sample sent from ``mem[j]`` arrives at ``mem[(j + D) mod depth]``, and D is the delay.**
It is read off a window header and one sample value — no timestamps, no correlator, no cross-spectrum.
For channel sounding that reading *is* the measurement.

This is the first thing in the repo to exercise the reason
:class:`~waveflow.hw.rfdc.Rfdc` carries both directions in one module: *"the TX and RX sample
counters must hold a fixed relation, and that is a property of the converter."*  Two converters would
be two epochs, and the address difference would mean nothing until they were tied together.

It aliases at ``depth``
-----------------------
The reading is a difference of addresses, so it is only ever known **modulo the buffer**.  A path
delay of ``D`` and one of ``D + depth * samp_per_word`` produce the *identical* capture, and no
amount of looking at it will separate them.  :func:`check_the_reading_aliases` drives that as a gate
rather than leaving it as a caveat, and the geometry here is chosen against the delay being shown:
one buffer is :data:`NSAMP` samples and the demonstrated delay is :data:`DELAY_SAMP`.

The two sources of an offset, and why the example drives both
-------------------------------------------------------------
An RX address offset has **two** causes and the address cannot tell them apart:

* a **path** delay — :attr:`~waveflow.simulation.rf_tb.RfSampDelay.delay_samp`.  *This path delivers
  later.*
* an **epoch** offset — ``t0`` on the converter.  *This tile's counter started later.*

Both move the reading by exactly the same amount, and
:func:`check_an_epoch_offset_moves_the_reading_too` shows it.  **That is what MTS buys you**: it pins
the second to zero, so what is left in the address is the first.  ``Rfdc`` models a non-zero value as
*"a tile deliberately started late, or a measured MTS residual"*, so this example sets both epochs
explicitly rather than inheriting a default — an assumption you cannot see is an assumption you will
forget you made.

There is also a **structural** term, and it is declared rather than fitted: a converter cannot emit
samples it has not yet collected, so one block exists at its grid tick and is transmitted across the
following period.  :attr:`RfShotLoopbackTB.loop_blk_latency` sums that hop with what the nodes on the
path declare, exactly as ``examples/rf_loopback`` does, and :func:`channel_delay` subtracts it.  What
is left equals what was configured.

Why the loads are spaced
------------------------
Under ``absolute_index`` a playout is deferred to the next buffer boundary, and **a load arriving
inside that window cancels the arm outright** — every command still answers ``SHOT_LOADED`` and
nothing reaches the air.  ``examples/rf_shot_tx``'s ``cmd_loop`` scenario is the demonstration: at
``absolute_index = 1`` it plays *nothing*.  So the two loads here are separated by
:data:`N_SPACER` refused frames whose payloads have to drain, which is what buys the transmitter a
whole pass of airtime between them.  Four is the measured threshold at this geometry and this uses
eight — see :data:`N_SPACER`.

**A third example, not a replacement.**  ``examples/rf_shot_tx`` and ``examples/rf_shot_rx`` stay.
A loopback cannot isolate a TX defect from an RX one — a wrong address here could be either end — so
those two gate sets remain the per-design contracts and this one adds only the **pair's** claims.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

import numpy as np

from waveflow.build.composite_gen import RFSOC4X2_CLK_HZ
from waveflow.hw.clock import Clock
from waveflow.hw.hw_freerun import FreeRunMod
from waveflow.hw.interface import StreamIF
from waveflow.hw.rf_relayout import to_dense
from waveflow.hw.rf_sample_if import RFSampIF
from waveflow.hw.rf_shot_rx import (
    CAP_OK,
    N_REGION,
    RfShotRx,
    split_windows,
    window_abs_index,
)
from waveflow.hw.rf_shot_tx import (
    SHOT_LOADED,
    SHOT_LOOP,
    SHOT_BAD_OPCODE,
    RfShotTx,
    shot_tx_schemas,
)
from waveflow.hw.rfdc import Rfdc
from waveflow.hw.rfdc_samp_word import Rfsoc4x2SampWord, pack
from waveflow.simulation.rf_tb import RfSampDelay
from waveflow.simulation.simulation import Simulation
from waveflow.simulation.stream_tb import StreamDriver, StreamSink

HERE = Path(__file__).resolve().parent

#: The converter's word: four 14-in-16 samples in 64 bits.  ``justify_shift() == 2``, so both
#: re-layout stages are real conversions rather than pairs of wires.
WORD = Rfsoc4x2SampWord.specialize(samp_per_word=4)
WORD_BW = int(WORD.bitwidth)
SPW = int(WORD.samp_per_word)

#: Memory depth in **words**, and **the same number on both ends**.  That is the whole reason the
#: correspondence is one statement: TX writes sample *j* at ``mem[j mod depth]`` and RX captures
#: sample *k* at ``mem[k mod depth]``, so *"sent from ``mem[j]``, arrives at ``mem[(j + D) mod
#: depth]``"* needs one ``depth`` and not two.  Two different depths would still each be a timestamp
#: — they would just be timestamps on different clocks.
DEPTH = 64
#: Samples in one buffer — the modulus the whole example is read against.
NSAMP = DEPTH * SPW
#: The receiver splits its memory into ``N_REGION`` regions; each announced window is one of them.
REGION_WORDS = DEPTH // N_REGION
REGION_SAMPLES = REGION_WORDS * SPW

#: Samples per converter block, and the same number in words.  One number for the transmitter's
#: chunk, the receiver's chunk, both re-layouts and the RF grid, because they are one quantum.
BLKSIZE = 64
BLK_WORDS = BLKSIZE // SPW

#: The converter's sample rate against a 250 MHz fabric — 0.256 words per cycle in each direction.
SAMP_RATE = 256e6

#: Blocks the RF grids run for.  Long enough that the second waveform gets airtime after the first,
#: and that several whole windows of each reach the host.
N_BLK = 90

#: **The delay this example demonstrates**, in samples.  Chosen to be awkward on purpose: 96 is not a
#: multiple of :data:`BLKSIZE` (64) and not a multiple of :data:`REGION_SAMPLES` (128), so a reading
#: that came out right could not have come from a block index or a window index — only from the
#: sample-granular address.  It is a multiple of :data:`SPW`, because an address is a **word** and a
#: delay that split a word would not be expressible as one.
DELAY_SAMP = 96

#: A delay one whole buffer longer than :data:`DELAY_SAMP`, for the aliasing gate.  The capture it
#: produces is **identical** — that is the point, and it is asserted rather than described.
ALIAS_DELAY_SAMP = DELAY_SAMP + NSAMP

#: Refused frames between the two loads, to buy the transmitter a pass of airtime.  **Measured, not
#: guessed**: at this geometry a full-payload refusal buys roughly a sixth of a pass, so the first
#: waveform reaches the air at four spacers and not at two.  Eight is a margin, and the cost of
#: getting it wrong is silent — every command still answers ``SHOT_LOADED`` and nothing plays.
N_SPACER = 8

#: The two waveforms' base codes.  **Non-zero, and that is load-bearing**:
#: :data:`~waveflow.hw.rf_shot_tx.FILLER` is 0, so a waveform whose first sample were zero would make
#: the filler-to-signal transition ambiguous in the log *and* in the figure.  Far apart, so which
#: waveform a sample belongs to is decidable from the sample alone.
CODE_A = 1000
CODE_B = 5000
KNOWN_BASES = (CODE_A, CODE_B)

#: An opcode this design does not know.  ``OPCODE_BW`` is 2 bits and the legal values are 0, 1 and 2,
#: so 3 is the only illegal one the wire can carry — which is what makes a spacer frame a *refusal*
#: rather than a malformed transfer.
BAD_OPCODE = 3

#: The header/response pair for THIS geometry, not the module defaults.
HDR, RESP = shot_tx_schemas(DEPTH, SPW)


# ---------------------------------------------------------------------------
# The waveforms and the command stream
# ---------------------------------------------------------------------------

def waveform(base: int) -> np.ndarray:
    """One whole buffer of distinguishable converter codes, as signed integers.

    A ramp of exactly :data:`NSAMP` codes, so ``code - base`` **is** the sample's index inside the
    waveform and the index is recoverable from any single sample.  That recovery is the measurement:
    a captured code names the address it was sent from, and its own address is where it arrived.
    """
    return np.arange(int(base), int(base) + NSAMP, dtype=np.int64)


def dense_words(base: int) -> np.ndarray:
    """The same waveform as the **dense words a host loads** — the memory's own format.

    Through the converter word's packer and then the re-layout's own converter, so this function
    invents no layout: the transmitter's memory holds dense words and its last stage makes them
    converter slots.
    """
    slots = np.asarray(pack(WORD, waveform(base).reshape(1, -1)), dtype=np.uint64).ravel()
    return np.asarray(to_dense(WORD, slots), dtype=np.uint64).ravel()


def frame(opcode: int, tid: int, nrepeat: int, payload: np.ndarray) -> np.ndarray:
    """One ``TLAST``-delimited frame: the header, then the samples."""
    h = HDR()
    h.opcode, h.tid, h.nrepeat = int(opcode), int(tid), int(nrepeat)
    return np.concatenate([np.asarray(h.serialize(word_bw=WORD_BW), dtype=np.uint64).ravel(),
                           np.asarray(payload, dtype=np.uint64).ravel()])


def scenario_frames(n_spacer: int = N_SPACER) -> list[np.ndarray]:
    """Waveform A, *n_spacer* refused frames, then waveform B — see *Why the loads are spaced*.

    The spacers carry a **full payload** deliberately: a refusal with no payload is one word and buys
    no time at all, and time is the only thing they are here for.
    """
    a, b = dense_words(CODE_A), dense_words(CODE_B)
    return ([frame(SHOT_LOOP, 0, 1, a)]
            + [frame(BAD_OPCODE, 1 + i, 1, a) for i in range(int(n_spacer))]
            + [frame(SHOT_LOOP, 100, 1, b)])


def expected_responses(frames) -> list[tuple[int, int]]:
    """``(tid, status)`` the transmitter must answer, **derived from the frames**."""
    out = []
    for f in frames:
        h = HDR().deserialize(np.asarray(f, dtype=np.uint64).ravel()[:HDR.nwords_per_inst(WORD_BW)],
                              word_bw=WORD_BW)
        out.append((int(h.tid),
                    SHOT_LOADED if int(h.opcode) == SHOT_LOOP else SHOT_BAD_OPCODE))
    return out


def write_scenario(root, frames=None, name: str = "cmd") -> None:
    """Materialize ``<root>/vectors/<name>`` — the frames the run drives in."""
    from waveflow.utils.burst_io import write_burst_bundle

    write_burst_bundle(list(scenario_frames() if frames is None else frames),
                       Path(root) / "vectors" / name)


# ---------------------------------------------------------------------------
# The graph
# ---------------------------------------------------------------------------

@dataclass
class RfShotLoopbackTB(FreeRunMod):
    """One converter, both buffers, and a path with a delay in it.

    Five participants and six edges.  Two of the edges are
    :class:`~waveflow.hw.rf_sample_if.RFSampIF` — the RF domain, one per direction — and the node
    between them is the **path**: :class:`~waveflow.simulation.rf_tb.RfSampDelay`.  The rest are
    ``StreamIF`` in the PL domain.

    **The converter is one node on purpose.**  ``Rfdc`` sets ``t0`` on every interface it binds, so
    the DAC's grid and the ADC's grid share an origin *structurally* rather than by two testbench
    fields that happen to agree.  That is what makes an address difference mean a delay.
    """

    #: Blocks each RF grid runs for.
    n_blk: int = N_BLK
    #: Samples of bulk delay on the path from the DAC to the ADC.
    delay_samp: int = DELAY_SAMP
    #: The **transmit** tile's epoch, in seconds.  Set explicitly rather than defaulted: the address
    #: correspondence rests on the two being equal, and an assumption you cannot see in the graph is
    #: one you will forget you made.
    t0_tx: float = 0.0
    #: The **receive** tile's epoch.  Equal to :attr:`t0_tx` is what MTS gives you; a non-zero
    #: difference is *"a tile deliberately started late, or a measured MTS residual"*.
    t0_rx: float = 0.0
    #: **The mode both buffers are built with.**  ``1`` is what this example is for; ``0`` is the
    #: negative control, and it is a knob rather than a second graph because what has to be isolated
    #: is the *parameter* — a second graph would differ in more than the thing under test.
    #:
    #: There is no per-end setting on purpose: a loopback with one end absolute and the other
    #: relative would capture perfectly good samples and read an address difference that means
    #: nothing, and making that unreachable by construction is cheaper than gating it.
    absolute_index: int = 1
    #: Which scenario bundle the driver plays.
    in_bundle: str = "vectors/cmd"
    axis_clk: Clock = field(default_factory=lambda: Clock(freq=RFSOC4X2_CLK_HZ))

    potential_targets: ClassVar[frozenset[str]] = frozenset()

    def __post_init__(self) -> None:
        super().__post_init__()
        self.axis_clk = Clock(name=f"{self.name}_axis_clk", freq=RFSOC4X2_CLK_HZ)
        self.samp_clk = Clock(name=f"{self.name}_samp_clk", freq=SAMP_RATE)
        #: Seconds per block on both RF grids.
        self.blk_period = BLKSIZE / SAMP_RATE

        # ONE converter, both directions -- and it owns both epochs.
        self.rfdc = Rfdc(name=f"{self.name}_rfdc", sim=self.sim, n_rx=1, n_tx=1, word=WORD,
                         t0_rx=float(self.t0_rx), t0_tx=float(self.t0_tx))
        w = self.rfdc.axis_bitwidth

        # BOTH buffers absolute.  A loopback with one of them relative would still capture the right
        # samples and would read a meaningless address difference, which is the failure this example
        # exists to make impossible to reach by accident.
        abs_idx = int(self.absolute_index)
        self.tx = RfShotTx.for_word(WORD, depth=DEPTH, sim=self.sim, name=f"{self.name}_tx",
                                    clk=self.axis_clk, blk_words=BLK_WORDS,
                                    absolute_index=abs_idx)
        self.rx = RfShotRx.for_word(WORD, depth=DEPTH, sim=self.sim, name=f"{self.name}_rx",
                                    clk=self.axis_clk, blk_words=BLK_WORDS,
                                    absolute_index=abs_idx)
        self.chan = RfSampDelay(name=f"{self.name}_chan", sim=self.sim,
                                delay_samp=int(self.delay_samp))

        self.drv = StreamDriver(sim=self.sim, name=f"{self.name}_drv", bitwidth=w,
                                in_bundle=str(self.in_bundle), has_tlast=True)
        self.resp_snk = StreamSink(sim=self.sim, name=f"{self.name}_resp_snk", bitwidth=w,
                                   out_bundle="vectors/resp", has_tlast=True)
        # queue_size, not the channel depth: a window frame is one header plus REGION_WORDS samples,
        # and a sink that stalled mid-window would make the RECEIVER drop -- which under absolute
        # indexing costs a whole window and would be measuring the testbench.
        self.win_snk = StreamSink(sim=self.sim, name=f"{self.name}_win_snk", bitwidth=w,
                                  out_bundle="vectors/win", has_tlast=True,
                                  queue_size=4 * (REGION_WORDS + 1))
        for c in (self.tx, self.rx, self.chan, self.rfdc, self.drv, self.resp_snk, self.win_snk):
            self.add_comp(c)

        # --- the RF domain: two edges and the PATH between them --------------------------------
        # The delay is on the NODE, never on either edge.  `rf_sample_if`'s own docstring draws that
        # line -- "if the edge can only record a quantity and never apply it, it does not belong on
        # the edge" -- and the reason it matters here is that the reader has to be able to tell a
        # path delay from an epoch offset, which is only possible if they are two different things.
        self.dac_if = RFSampIF(name=f"{self.name}_dac_if", sim=self.sim, samp_clk=self.samp_clk,
                               n_ch=1, blksize=BLKSIZE, n_blk=int(self.n_blk))
        self.dac_if.bind("tx", self.rfdc.tx_rf)
        self.dac_if.bind("rx", self.chan.rf_in)
        self.add_if(self.dac_if)

        self.adc_if = RFSampIF(name=f"{self.name}_adc_if", sim=self.sim, samp_clk=self.samp_clk,
                               n_ch=1, blksize=BLKSIZE, n_blk=int(self.n_blk))
        self.adc_if.bind("tx", self.chan.rf_out)
        self.adc_if.bind("rx", self.rfdc.rx_rf)
        self.add_if(self.adc_if)

        # --- the PL domain ----------------------------------------------------------------------
        cmd_words = DEPTH + 1                    # one ShotTxHdr, then the payload
        for nm, master, slave, depth in (
                ("cmd", self.drv.stream_ep, self.tx.s_in, 2 * cmd_words),
                ("resp", self.tx.resp_out, self.resp_snk.stream_ep, None),
                ("dac", self.tx.samp_out, self.rfdc.tx_streams[0], 2 * BLK_WORDS),
                ("adc", self.rfdc.rx_streams[0], self.rx.samp_in, None),
                ("win", self.rx.w_out, self.win_snk.stream_ep, None)):
            kw = {} if depth is None else {"depth": int(depth)}
            ifc = StreamIF(name=f"{self.name}_{nm}_axis", sim=self.sim, clk=self.axis_clk,
                           bitwidth=w, **kw)
            ifc.bind("master", master)
            ifc.bind("slave", slave)
            self.add_if(ifc)
            setattr(self, f"{nm}_axis", ifc)

    # -- what the graph declares about itself ---------------------------------------------------

    @property
    def loop_blk_latency(self) -> int:
        """Block periods between a sample leaving the transmitter and reaching the receiver,
        **summed from what the nodes on the path declare** rather than fitted to a measurement.

        Two terms, and the first is not the path's:

        * **one block for the converter hop.**  A converter cannot emit samples it has not yet
          collected, so a block exists at its grid tick and is transmitted across the *following*
          period.  ``examples/rf_loopback`` states the same quantity the same way, and it is the
          same rule as the fidelity contract's *"no dependency shorter than 2*blksize — one block
          per converter hop"*.
        * **whatever the path declares** —
          :attr:`~waveflow.simulation.rf_tb.RfSampDelay.blk_latency`, which is zero: the node emits
          in the SimPy event it received in.  A zero that is stated can be summed; one that is
          merely true cannot.

        :attr:`~waveflow.simulation.rf_tb.RfSampDelay.delay_samp` is **not** in here, and that is
        the point: this is the loop's structural cost, and the delay is what is left after it is
        taken off.
        """
        return 1 + int(self.chan.blk_latency)

    @property
    def loop_latency_samp(self) -> int:
        """:attr:`loop_blk_latency` in samples — what :func:`channel_delay` subtracts."""
        return int(self.loop_blk_latency) * BLKSIZE

    @property
    def epoch_offset_samp(self) -> int:
        """``t0_rx - t0_tx`` in samples — **how much later the receiving tile's counter started**.

        A receive tile that started later gives an arriving sample a *smaller* index of its own, so
        it makes the address difference **shrink**; a transmit tile that started later makes it grow.
        Both by exactly this many samples, which is the whole point of
        :func:`check_an_epoch_offset_moves_the_reading_too`.

        **Zero is what MTS gives you.**  Anything else is *"a tile deliberately started late, or a
        measured MTS residual"*, and :func:`channel_delay` has to take it back off before what is
        left is a property of the path.
        """
        return int(round((float(self.t0_rx) - float(self.t0_tx)) * SAMP_RATE))

    @property
    def run_until(self) -> float:
        """Simulated horizon: the grids' own length plus a two-block tail.

        A testbench constant, not a latency — both converters are free-running event sources that
        never exhaust, so ``env.run()`` with no bound would never return.
        """
        return (int(self.n_blk) + 2) * self.blk_period


# ---------------------------------------------------------------------------
# Running it, and reading what came out
# ---------------------------------------------------------------------------

def run_pysim(root=None, frames=None, **kw) -> RfShotLoopbackTB:
    """Build the graph, run it to the grids' horizon, return the testbench."""
    import tempfile

    tb = RfShotLoopbackTB(name="tb", sim=Simulation(), **kw)
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(root or tmp)
        write_scenario(base, frames, name=Path(tb.in_bundle).name)
        tb.drv.root = base
        tb.resp_snk.root = base
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


def window_frames(tb: RfShotLoopbackTB) -> list[np.ndarray]:
    """The raw window frames the host sink collected — header **and** samples."""
    return [np.asarray(b, dtype=np.uint64).ravel() for b in tb.win_snk.words]


def responses(tb: RfShotLoopbackTB) -> list[tuple[int, int]]:
    """``(tid, status)`` off the response **stream**, in arrival order."""
    words = np.concatenate([np.asarray(b).ravel() for b in tb.resp_snk.words])
    n = RESP.nwords_per_inst(WORD_BW)
    out = []
    for i in range(0, words.size - n + 1, n):
        r = RESP().deserialize(words[i:i + n], word_bw=WORD_BW)
        out.append((int(r.tid), int(r.status)))
    return out


def windows_as_codes(frames) -> list[tuple[object, np.ndarray]]:
    """``[(hdr, codes), ...]`` — each window's header and its samples as signed converter codes.

    The header is split off by the design's own reader of its own layout and the samples come back
    through the dense element's serializer, so this function invents nothing.
    """
    from waveflow.hw.arrayutils import read_array
    from waveflow.hw.rf_relayout import dense_elem_type

    out = []
    for hdr, words in split_windows(frames, WORD_BW):
        n = int(words.size) * SPW
        vals = read_array(np.asarray(words, dtype=np.uint64), elem_type=dense_elem_type(WORD),
                          word_bw=WORD_BW, shape=n)
        out.append((hdr, np.asarray(getattr(vals, "val", vals), dtype=np.int64).ravel()))
    return out


# ---------------------------------------------------------------------------
# THE MEASUREMENT
# ---------------------------------------------------------------------------

def address_differences(frames) -> dict[int, int]:
    """``{address difference: how many samples said so}`` — the whole measurement, in five lines.

    For each captured sample:

    * **where it arrived** is ``window_abs_index(...) * samp_per_word + offset``, modulo
      :data:`NSAMP` — the receiver's own absolute index, taken from the window header and nothing
      else;
    * **where it was sent from** is ``code - base``, because the waveform is a ramp filling exactly
      one buffer, so a code names its address in the transmitter's memory;
    * the difference of the two, modulo :data:`NSAMP`, is the delay.

    Filler (:data:`~waveflow.hw.rf_shot_tx.FILLER`, zero) is skipped — it was never sent from an
    address.  A code belonging to no known waveform is a **failure**, not a skip: it means the
    capture holds something neither end put there.
    """
    got: dict[int, int] = {}
    for w, (hdr, codes) in enumerate(windows_as_codes(frames)):
        k = window_abs_index(w, int(hdr.n_dropped), REGION_WORDS)
        for off, v in enumerate(np.asarray(codes, dtype=np.int64).tolist()):
            if v == 0:
                continue
            base = next((b for b in KNOWN_BASES if b <= v < b + NSAMP), None)
            if base is None:
                raise AssertionError(
                    f"window {w} holds code {v} at offset {off}, which belongs to neither loaded "
                    f"waveform {list(KNOWN_BASES)}. The capture contains something no end of this "
                    f"loopback put there.")
            sent_from = (v - base) % NSAMP
            arrived_at = (k * SPW + off) % NSAMP
            d = (arrived_at - sent_from) % NSAMP
            got[d] = got.get(d, 0) + 1
    return got


def measured_delay(frames, *, where: str = "") -> int:
    """The one address difference every captured sample agrees on.

    **Unanimity is the assertion, not an implementation detail.**  Two different differences in one
    run means the two ends disagree about phase somewhere — the transmitter slipped a pass, the
    receiver mis-addressed a window, or a block was lost and the capture closed over the hole — and
    every one of those produces a perfectly plausible-looking capture.  A single value is the only
    reading that says the pair is in step.
    """
    got = address_differences(frames)
    if not got:
        raise AssertionError(
            f"{where}no captured sample carried a waveform, so there is no delay to read. The "
            f"transmitter played nothing, or nothing traversed the path.")
    if len(got) != 1:
        raise AssertionError(
            f"{where}the captured samples do not agree on one address difference: {got} "
            f"(difference -> sample count). The two ends are not in step.")
    return next(iter(got))


def channel_delay(tb: RfShotLoopbackTB, frames, *, where: str = "") -> int:
    """**The headline.**  The path's delay in samples, read off the addresses.

    The raw address difference minus the loop's own declared structural latency
    (:attr:`RfShotLoopbackTB.loop_latency_samp`) and minus the epoch offset the converter was built
    with.  What is left is what the path was configured with — and it is only ever known modulo
    :data:`NSAMP`, which :func:`check_the_reading_aliases` makes a gate rather than a caveat.
    """
    raw = measured_delay(frames, where=where)
    return (raw - tb.loop_latency_samp + tb.epoch_offset_samp) % NSAMP


# ---------------------------------------------------------------------------
# The checks
# ---------------------------------------------------------------------------

def check_the_epochs_are_tied(tb: RfShotLoopbackTB, *, where: str = "") -> None:
    """One converter, one epoch — the assumption the address correspondence rests on.

    Asserted against the **interfaces**, not against the testbench's own fields: ``Rfdc`` pushes
    ``t0`` onto every edge it binds, and it is that push, not the constructor argument, that decides
    when each grid ticks.
    """
    if tb.dac_if.t0 != float(tb.t0_tx) or tb.adc_if.t0 != float(tb.t0_rx):
        raise AssertionError(
            f"{where}the converter did not push the epochs this graph asked for: dac_if.t0="
            f"{tb.dac_if.t0} (asked {tb.t0_tx}), adc_if.t0={tb.adc_if.t0} (asked {tb.t0_rx}).")
    if tb.epoch_offset_samp and tb.t0_rx == tb.t0_tx:
        raise AssertionError(f"{where}epoch_offset_samp is inconsistent with t0_rx == t0_tx")


def check_the_run_was_clean(tb: RfShotLoopbackTB, frames, *, where: str = "") -> None:
    """Nothing was lost anywhere on the loop, so the reading is a delay and not a hole.

    Four counters and each is a different failure.  **The receiver's is the one that matters most
    under absolute indexing**: a stalled reader there costs a *whole window* rather than a block, so
    a demonstration fighting drops would be measuring the sink.
    """
    tb.chan.assert_ran(where=where)
    if int(tb.rx.n_dropped):
        raise AssertionError(
            f"{where}the receiver dropped {int(tb.rx.n_dropped)} word(s) — under absolute indexing "
            f"that is a whole window at a time, and this demonstration is supposed to be measuring "
            f"a delay rather than a starved host.")
    if int(tb.dac_if.overrun) or int(tb.adc_if.overrun):
        raise AssertionError(
            f"{where}an RF edge overran (dac={int(tb.dac_if.overrun)}, "
            f"adc={int(tb.adc_if.overrun)}): a block was presented and refused, so the capture has "
            f"a gap the addresses cannot show.")
    if int(tb.dac_if.underrun):
        raise AssertionError(
            f"{where}the DAC edge underran {int(tb.dac_if.underrun)} time(s) — the transmitter did "
            f"not keep the converter fed, which is a fault in the design rather than in the loop.")
    bad = [(t, s) for (t, s), (wt, ws) in zip(responses(tb), expected_responses(frames),
                                              strict=True) if (t, s) != (wt, ws)]
    if bad:
        raise AssertionError(f"{where}the transmitter answered {responses(tb)}, expected "
                             f"{expected_responses(frames)}")


def check_both_waveforms_reached_the_air(frames, *, where: str = "") -> int:
    """Both loads played — which is what says the spacing was enough.

    Under ``absolute_index`` a load arriving inside the deferral window is cancelled outright and
    **every command still answers ``SHOT_LOADED``**, so the response stream cannot tell you this and
    only the capture can.  Returns the number of windows carrying the first waveform.
    """
    seen = set()
    n_a = 0
    for hdr, codes in windows_as_codes(frames):
        for v in np.asarray(codes, dtype=np.int64).tolist():
            if v == 0:
                continue
            b = next((b for b in KNOWN_BASES if b <= v < b + NSAMP), None)
            if b is not None:
                seen.add(b)
        if any(CODE_A <= v < CODE_A + NSAMP for v in np.asarray(codes).tolist()):
            n_a += 1
    missing = set(KNOWN_BASES) - seen
    if missing:
        raise AssertionError(
            f"{where}waveform(s) {sorted(missing)} never reached the air. Under absolute_index a "
            f"load arriving inside the deferral window is cancelled and answered SHOT_LOADED "
            f"anyway, so this is what a too-close pair of loads looks like — raise N_SPACER.")
    return n_a


def check_every_window_is_whole_and_announced_clean(frames, *, where: str = "") -> None:
    """Every window is one full region and says it lost nothing.

    Not a re-gate of ``examples/rf_shot_rx`` — that one proves the receiver's contract against a
    ramp it can see whole.  What this asserts is the **precondition of the measurement**: a short or
    ``CAP_LOST`` window would make ``window_abs_index`` name an address the samples are not at.
    """
    wins = windows_as_codes(frames)
    if not wins:
        raise AssertionError(f"{where}no window reached the host.")
    sizes = {int(c.size) for _h, c in wins}
    if sizes != {REGION_SAMPLES}:
        raise AssertionError(
            f"{where}windows of {sorted(sizes)} samples, expected only {REGION_SAMPLES}.")
    bad = [(i, int(h.status), int(h.n_dropped)) for i, (h, _c) in enumerate(wins)
           if int(h.status) != CAP_OK or int(h.n_dropped)]
    if bad:
        raise AssertionError(f"{where}window(s) {bad} carry a loss verdict (index, status, "
                             f"n_dropped); the addresses below would then name a hole.")


def leading_silent_windows(frames) -> int:
    """How many windows arrived before the first sample did — the path's length **in time**.

    The only thing in a capture that a longer path changes once the loop is in steady state, and
    therefore the thing :func:`check_the_reading_aliases` uses to prove the two runs it compares are
    genuinely different runs.
    """
    n = 0
    for _hdr, codes in windows_as_codes(frames):
        if np.any(np.asarray(codes) != 0):
            break
        n += 1
    return n


def check_the_reading_aliases(*, where: str = "") -> tuple[int, int]:
    """**The rule's other half**, and an example that skipped it would teach half of one.

    A path delay of ``D`` and one of ``D + NSAMP`` read **the same address difference**, because the
    reading *is* a difference of addresses and an address lives modulo the buffer. So the geometry
    has to be chosen against the delay being measured; there is nothing in the number that could
    warn you.

    **What the two runs do NOT agree about is when the first sample arrived**, and that is asserted
    here too — it is what says these are two different runs rather than one run compared with
    itself, and it names exactly the information an address discards. A longer path holds more
    silence at the start; a timestamped measurement would see that, and this one deliberately does
    not look at it.

    Returns ``(reading at DELAY_SAMP, reading at ALIAS_DELAY_SAMP)``.
    """
    near = run_pysim(delay_samp=DELAY_SAMP)
    far = run_pysim(delay_samp=ALIAS_DELAY_SAMP)
    fn, ff = window_frames(near), window_frames(far)

    dn = channel_delay(near, fn, where=where)
    df = channel_delay(far, ff, where=where)
    if dn != df or dn != DELAY_SAMP % NSAMP:
        raise AssertionError(
            f"{where}delay {DELAY_SAMP} read {dn} and delay {ALIAS_DELAY_SAMP} read {df}; both "
            f"should read {DELAY_SAMP % NSAMP}. An address difference aliases at one buffer.")

    ln, lf = leading_silent_windows(fn), leading_silent_windows(ff)
    if lf <= ln:
        raise AssertionError(
            f"{where}the longer path did not hold more silence at the start ({lf} silent window(s) "
            f"against {ln}), so these two runs are not distinguishable at all and the aliasing "
            f"claim above is comparing a run with itself.")
    return dn, df


def check_an_epoch_offset_moves_the_reading_too(*, blocks: int = 1,
                                                where: str = "") -> tuple[int, int]:
    """**What MTS buys you**, shown by taking it away.

    The path is untouched and the **transmitting tile's counter is started one block late**.  The
    raw address difference moves by exactly that many samples — the same move a longer path would
    have produced, and the capture cannot tell you which it was.  ``t0`` is what pins the epoch to
    zero so that what is left in the address is the path; that is the whole content of *"``t0_tx ≡
    t0_rx`` is what MTS gives you"*.

    **``t0_tx``, not ``t0_rx``, and the asymmetry is a property of the model rather than a
    preference.**  Starting the *receive* tile later can only cancel structural latency the loop
    already has, and once that is spent the block simply waits in a queue — so past one block a
    later receiver stops moving the reading. Starting the *transmit* tile later has no such floor.
    It is the block-LT resolution limit showing through, and a gate that swept ``t0_rx`` would be
    asserting a saturation curve rather than a correspondence.

    Returns ``(raw reading with the epochs tied, raw reading with the transmitter late)``.
    """
    tied = run_pysim()
    late = run_pysim(t0_tx=int(blocks) * (BLKSIZE / SAMP_RATE))
    r_tied = measured_delay(window_frames(tied), where=where)
    r_late = measured_delay(window_frames(late), where=where)
    want = (r_tied + int(blocks) * BLKSIZE) % NSAMP
    if r_late != want:
        raise AssertionError(
            f"{where}with the transmit tile started {blocks} block(s) late the reading is "
            f"{r_late}; the epochs being tied it is {r_tied}, so it should be {want}. An epoch "
            f"offset moves the address difference exactly as a path delay does.")
    # ... and once the epoch is taken back off, the PATH still reads what it was configured with.
    d_late = channel_delay(late, window_frames(late), where=where)
    if d_late != DELAY_SAMP % NSAMP:
        raise AssertionError(
            f"{where}with the epoch offset subtracted the path reads {d_late}, configured "
            f"{DELAY_SAMP}. Separating the two is the only thing that makes either one meaningful.")
    return r_tied, r_late


def check_the_two_ends_agree_on_phase(frames, *, min_samples: int = 1024,
                                      where: str = "") -> tuple[int, int]:
    """**The pair's claim**, and the only place it can be made.

    S1 gates the transmitter's phase against its own memory and S2 gates the receiver's against its
    own; neither can say the two are the *same* phase, because neither has the other end. Here every
    captured sample is asked which address it was sent from and which it arrived at, and **all of
    them return the same difference**.

    Unanimity over every sample is what makes this a phase claim rather than a delay estimate. A
    transmitter that slipped a pass, a receiver that mis-addressed one window, or a block lost
    anywhere on the loop each produce a capture that still looks like a waveform and still passes
    every counter — and each puts a second value in this histogram.

    *min_samples* is a **non-vacuity floor**: a run in which four samples agreed would agree for no
    reason worth having.

    Returns ``(the agreed difference, how many samples agreed)``.
    """
    got = address_differences(frames)
    if len(got) != 1:
        raise AssertionError(
            f"{where}the two ends do not agree on one phase: {got} (address difference -> sample "
            f"count). Every entry beyond the first is a sample that arrived somewhere its own index "
            f"does not name.")
    d, n = next(iter(got.items()))
    if n < int(min_samples):
        raise AssertionError(
            f"{where}only {n} sample(s) carried a waveform, under the floor of {min_samples}. "
            f"Agreement over a handful of samples is not evidence of a phase relation.")
    return d, n
