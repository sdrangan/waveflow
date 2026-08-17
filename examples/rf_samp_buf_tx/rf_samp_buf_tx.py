"""rf_samp_buf_tx.py — the TX playout demo for :class:`~waveflow.hw.rf_samp_buf_tx.RfSampBufTx`.

The **module** is framework and lives in :mod:`waveflow.hw.rf_samp_buf_tx`; what is left here is what
an example should be — a graph that wires it to a real converter, a scenario that exercises every
case it claims to handle, and a predicted golden:

    StreamDriver --StreamIF--> s_in (TxCmd, then its payload IN-BAND)
    RfSampBufTx.s_out --StreamIF--> Rfdc.tx_stream | Rfdc.tx_rf --RFSampIF--> RfDataSink
    RfSampBufTx.s_resp --StreamIF--> StreamSink

**The converter is really here**, not a stand-in sink, because the one thing this design exists to
satisfy is that a DAC cannot be told to wait.  The tile is DAC-only (``n_rx=0``): a playout design
has no receiver, and wiring a fake ADC in would add a metronome nothing drains.

**The player starts before the loader**, and that is not a flaw in the scenario — it is what a real
DAC does.  The play pointer walks from sample 0 at *t=0* whether or not anything has been loaded, so
the first slots go out stale and are counted.  A real design primes its buffer before enabling the
tile; here the transient is left visible and **asserted**, because a counter that has never counted
is not evidence.
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
from waveflow.hw.rf_samp_buf import (  # noqa: E402
    BUF_DEPTH,  # noqa: F401  (re-exported: the RX default, for comparison)
    HORIZON_MARGIN,
    IDX_BW,
    RF_SAMP_BUF_MISALIGNED,
    RF_SAMP_BUF_OK,
    RF_SAMP_BUF_TOO_LATE,
    pack_samples,
    sdiff,
    unpack_samples,
)
from waveflow.hw.rf_samp_buf_tx import (  # noqa: E402
    TX_SCHEMA_CLASSES,
    RfSampBufLoader,
    RfSampBufPlayer,
    RfSampBufTx,
    TxCmd,
    TxResp,
)
from waveflow.hw.rf_sample_if import RFSampIF  # noqa: E402
from waveflow.simulation.rf_tb import RfDataSink  # noqa: E402
from waveflow.simulation.simulation import Simulation  # noqa: E402
from waveflow.simulation.stream_tb import StreamDriver, StreamSink  # noqa: E402

from examples.rf_loopback.rfdc import Rfdc  # noqa: E402

__all__ = [
    "BUF_DEPTH", "HORIZON_MARGIN", "IDX_BW", "RF_SAMP_BUF_MISALIGNED", "RF_SAMP_BUF_OK",
    "RF_SAMP_BUF_TOO_LATE", "TX_SCHEMA_CLASSES", "RfSampBufLoader", "RfSampBufPlayer",
    "RfSampBufTx", "TxCmd", "TxResp", "pack_samples", "sdiff", "unpack_samples",
    "GATE_COMMANDS", "LATE_TIDS", "PRIMED_AT", "SAMP_BASE", "SAMP_BW", "TX_BUF_DEPTH",
    "XSI_BLKSIZE",
    "XSI_NBLK", "XSI_NSAMP",
    "RfSampBufTxTB", "command_frame", "expected_responses", "find_loaded_run",
    "played_samples", "ramp_samples",
    "responses", "run_pysim", "write_scenario",
]

# ---------------------------------------------------------------------------
# The scenario
# ---------------------------------------------------------------------------

#: Buffer depth in WORDS for the TX demo — **twice the RX default, and that is a measured sizing
#: fact rather than a round number.**
#:
#: The player is free-running and the loader must stay in front of it, so the buffer has to cover the
#: loader's whole latency: the head start (:data:`PRIMED_AT`) plus everything in flight.  At the RX
#: default of 1024 words there is no room to prime four blocks ahead *and* keep the command inside
#: the buffer, and the RTL run showed exactly that failure -- the player overtook the loader and
#: commands came back ``TOO_LATE`` with a partial ``nloaded``.  A playout buffer that is too shallow
#: does not merely reduce margin; it makes the design not work.
TX_BUF_DEPTH = 2048

#: Sample width in bits.  A *sample* is always 16 bits here; ``samp_per_word`` changes how many ride
#: one AXIS word, not how wide one is.
SAMP_BW = 16

#: The gate scenario: 8 DAC blocks of 256 samples = 2048 sample periods of playout against a
#: 1024-word buffer, so the play pointer laps the buffer and the circular wrap is exercised rather
#: than assumed.
XSI_NBLK = 8
XSI_BLKSIZE = 256
XSI_NSAMP = XSI_NBLK * XSI_BLKSIZE

#: Sample *values* are a ramp, ``SAMP_BASE + i``, so every played word identifies the slot it came
#: from.  A constant would pass a player that emitted the wrong slot, which is the whole failure mode
#: a circular buffer has.
SAMP_BASE = 1000


def ramp_samples(nsamp: int = XSI_NSAMP, base: int = SAMP_BASE) -> np.ndarray:
    """The sample values a command carries: ``base + idx`` at sample index ``idx``."""
    return ((np.arange(int(nsamp), dtype=np.int64) + int(base)) % (1 << SAMP_BW)).astype(np.uint64)


#: Sample index the first command targets.  **Not zero, and the reason is the design**: the player
#: is free-running, so it walks slots 0.. from *t=0* whether or not anything has been loaded.  A
#: command must therefore aim far enough ahead that the loader wins the race, and this is the
#: startup priming a real design does before enabling the tile.  Everything below this index plays
#: stale and is counted — see :attr:`RfSampBufPlayer.n_underrun`.
PRIMED_AT = 1024

#: The gate commands, and **why each one is the case it claims to be**.  Determinism comes from the
#: ORDER: the loader serves one command at a time and the player advances monotonically, so each
#: command's case is implied by the completion of the one before it.
#:
#: 1. ``tid=1  start=1024 nsamp=512`` — **in the future, placed straight away**.  Issued at t=0 when
#:    the player is at slot 0; slot 1024 is four DAC blocks ahead, which is the head start the
#:    loader needs to stay in front of it — see :data:`TX_BUF_DEPTH` for why that is a SIZING fact.
#: 2. ``tid=2  start=1536 nsamp=512`` — **contiguous with the first**, so the played ramp has no seam
#:    across the hand-off — which is where a fill-pointer bug would show.
#: 3. ``tid=3  start=0    nsamp=4``   — **too late**.  By the time it is served the player is long
#:    past sample 0, so its slot has already gone out of the DAC and cannot be recalled.
#: 4. ``tid=4  start=2048 nsamp=6``   — **misaligned at samp_per_word=4** (1536 % 4 == 0 but 6 % 4
#:    != 0), and legal at 1 and 2.  Its status is therefore geometry-dependent, which is the point:
#:    the same command must be refused wherever a partial word would otherwise be silently rounded.
GATE_COMMANDS = (
    (1, PRIMED_AT, 512),
    (2, PRIMED_AT + 512, 512),
    (3, 0, 4),
    (4, PRIMED_AT + 1024, 6),
)


def command_frame(cmds=GATE_COMMANDS, samp_per_word: int = 1) -> list[np.ndarray]:
    """The in-band frame both backends play: for each command, its ``TxCmd`` burst then its payload.

    Two bursts per command rather than one, because that is what the framing means: the loader reads
    a fixed-size command and then exactly ``ceil(nsamp/spw)`` payload words.  Serialized through the
    schema and packed through the generated array serializer — never by hand, because slot order is
    unobservable at one sample per word.
    """
    spw = int(samp_per_word)
    word_bw = SAMP_BW * spw
    ramp = ramp_samples()
    out: list[np.ndarray] = []
    for tid, start, nsamp in cmds:
        out.append(np.asarray(TxCmd(tid=tid, start=start, nsamp=nsamp).serialize(word_bw=word_bw),
                              dtype=np.uint64))
        npay = (int(nsamp) + spw - 1) // spw
        # The payload is the ramp values for the samples this command places, padded up to a whole
        # number of words when nsamp is not one (a misaligned command still owes its frame).
        vals = np.array([int(ramp[(int(start) + i) % ramp.size]) for i in range(npay * spw)],
                        dtype=np.uint64)
        out.append(pack_samples(vals, word_bw=word_bw, samp_per_word=spw))
    return out


#: Transaction ids the scenario *designs* to arrive after their slot has played.  Stated rather than
#: observed: whether a window is too late depends on where the player has reached, which is timing,
#: so it is the scenario's argument (see :data:`GATE_COMMANDS`) that predicts it and the run that
#: has to agree — not the other way round.
LATE_TIDS = (3,)


def expected_responses(samp_per_word: int = 1, cmds=GATE_COMMANDS,
                       late_tids=LATE_TIDS) -> list[tuple[int, int, int]]:
    """The **predicted** ``(tid, status, nloaded)`` per command.

    Two of the three verdicts follow from the command alone — alignment is arithmetic on
    ``samp_per_word``, and everything else is OK — and the third, *too late*, is the scenario's
    stated design rather than a transcription of a run.  Alignment is checked first because a
    misaligned command is refused before its slot is ever examined.
    """
    spw = int(samp_per_word)
    out: list[tuple[int, int, int]] = []
    for tid, start, nsamp in cmds:
        if (start % spw) or (nsamp % spw):
            out.append((tid, RF_SAMP_BUF_MISALIGNED, 0))
        elif tid in late_tids:
            out.append((tid, RF_SAMP_BUF_TOO_LATE, 0))
        else:
            out.append((tid, RF_SAMP_BUF_OK, nsamp))
    return out


# ---------------------------------------------------------------------------
# The testbench graph
# ---------------------------------------------------------------------------

@dataclass
class RfSampBufTxTB(FreeRunMod):
    """A host loading the buffer, a player feeding a real DAC, two sinks collecting.

    The tile is DAC-only (``n_rx=0``).  ``samp_per_word=1`` is the gated geometry — the recorded RTL
    cycle count is for it — and larger values are exercised in pysim.
    """

    potential_targets: ClassVar[frozenset[str]] = frozenset({SEQUENTIAL_XSI_TB})

    n_blk: int = XSI_NBLK
    blksize: int = XSI_BLKSIZE
    #: 64 MSa/s on a 300 MHz fabric — 0.213 samples per cycle against a player that sustains 0.5.
    #: NOT a free parameter: see :meth:`check_rate`.
    samp_rate: float = 64e6
    axis_freq: float = RFSOC4X2_CLK_HZ
    nbits: int = SAMP_BW
    #: Samples per AXIS word.  **1 is the gated configuration.**
    samp_per_word: int = 1
    depth: int = TX_BUF_DEPTH
    horizon_margin: int = HORIZON_MARGIN
    #: Fixed run bound for the generated XSI main — a testbench constant, not a latency.
    n_cycles: int = 40000
    #: **Fault injection.**  ``False`` skips :meth:`check_rate`, so a DAC the player cannot feed can
    #: be wired up on purpose.  A counter that has never counted is not evidence, and the underrun
    #: this provokes is the only demonstration that the pysim twin models rate at all.
    enforce_rate: bool = True
    axis_clk: Clock = field(default_factory=lambda: Clock(freq=RFSOC4X2_CLK_HZ))

    def check_rate(self) -> float:
        """Refuse a sample rate the player cannot sustain, and return the utilisation.

        The arithmetic and the message live on the module
        (:meth:`~waveflow.hw.rf_samp_buf_tx.RfSampBufTx.check_rate`) because a module's throughput is
        part of its interface contract; what a testbench owns is the **pairing**.
        """
        if not self.enforce_rate:
            return float(self.samp_rate) / self.dut.max_samp_rate(float(self.axis_freq))
        return self.dut.check_rate(float(self.samp_rate), float(self.axis_freq))

    def __post_init__(self) -> None:
        super().__post_init__()
        self.axis_clk = Clock(name=f"{self.name}_axis_clk", freq=float(self.axis_freq))
        self.samp_clk = Clock(name=f"{self.name}_samp_clk", freq=float(self.samp_rate))
        self.blk_period = int(self.blksize) / float(self.samp_rate)

        self.rfdc = Rfdc(name=f"{self.name}_rfdc", sim=self.sim, n_rx=0, n_tx=1,
                         nbits=int(self.nbits), samp_per_word=int(self.samp_per_word))
        w = self.rfdc.axis_bitwidth
        self.dut = RfSampBufTx(name=f"{self.name}_dut", sim=self.sim, bitwidth=w,
                               samp_per_word=int(self.samp_per_word), depth=int(self.depth),
                               horizon_margin=int(self.horizon_margin),
                               # pysim's quantum on the converter edge is a BLOCK: Rfdc's DAC
                               # process takes one blksize burst per event.  A modelling shape only
                               # -- the rate is charged per word either way.
                               blk_words=int(self.blksize) // int(self.samp_per_word),
                               # The metronome, handed over directly: pysim cannot deliver it
                               # through the wire.  See RfSampBufPlayer.dac_word_rate.
                               dac_word_rate=float(self.samp_rate) / int(self.samp_per_word),
                               clk=self.axis_clk)
        #: Fraction of the player's capacity this scenario asks for — checked, not assumed.
        self.rate_util = self.check_rate()
        self.cmd_drv = StreamDriver(sim=self.sim, name=f"{self.name}_cmd_drv", bitwidth=w,
                                    in_bundle="vectors/cmd", has_tlast=True)
        self.sink = RfDataSink(name=f"{self.name}_sink", sim=self.sim, out_bundle="vectors/rf_out")
        self.resp_sink = StreamSink(sim=self.sim, name=f"{self.name}_resp_snk", bitwidth=w,
                                    out_bundle="vectors/resp", has_tlast=True)
        for c in (self.dut, self.rfdc, self.cmd_drv, self.sink, self.resp_sink):
            self.add_comp(c)

        # --- the RF domain: one interface, one metronome (there is no ADC) --------------------
        self.dac_if = RFSampIF(name=f"{self.name}_dac_if", sim=self.sim, samp_clk=self.samp_clk,
                               n_ch=1, blksize=int(self.blksize), n_blk=int(self.n_blk))
        self.dac_if.bind("tx", self.rfdc.tx_rf)
        self.dac_if.bind("rx", self.sink.rf_ep)
        self.add_if(self.dac_if)

        # --- the PL domain --------------------------------------------------------------------
        # No depth overrides: these become the DUT's top-level AXIS ports, and a top-level argument
        # cannot carry a FIFO depth (Vitis ignores the pragma).  The elasticity that matters is
        # inside the design -- and here it is the circular buffer itself.
        cmd_axis = StreamIF(name=f"{self.name}_cmd_axis", sim=self.sim, clk=self.axis_clk,
                            bitwidth=w)
        cmd_axis.bind("master", self.cmd_drv.stream_ep)
        cmd_axis.bind("slave", self.dut.s_in)
        self.add_if(cmd_axis)

        # No depth override, and it would not help: pysim does not back-pressure a burst write, so
        # a queue bound cannot pace the player.  The metronome is handed to it directly instead --
        # see RfSampBufPlayer.dac_word_rate, where the measurement is recorded.
        self.dac_axis = StreamIF(name=f"{self.name}_dac_axis", sim=self.sim, clk=self.axis_clk,
                                 bitwidth=w)
        self.dac_axis.bind("master", self.dut.s_out)
        self.dac_axis.bind("slave", self.rfdc.tx_stream)
        self.add_if(self.dac_axis)

        resp_axis = StreamIF(name=f"{self.name}_resp_axis", sim=self.sim, clk=self.axis_clk,
                             bitwidth=w)
        resp_axis.bind("master", self.dut.s_resp)
        resp_axis.bind("slave", self.resp_sink.stream_ep)
        self.add_if(resp_axis)


def write_scenario(root, samp_per_word: int = 1, cmds=GATE_COMMANDS) -> None:
    """Materialize ``<root>/vectors/cmd`` — the in-band frame BOTH backends play.

    One writer, so the RTL run and the pysim golden cannot start from different bytes.
    """
    from waveflow.utils.burst_io import write_burst_bundle

    write_burst_bundle(command_frame(cmds, samp_per_word), Path(root) / "vectors" / "cmd")


def run_pysim(root=None, tb: "RfSampBufTxTB | None" = None, cmds=GATE_COMMANDS) -> "RfSampBufTxTB":
    """Run the graph in SimPy and return the testbench, its sinks holding what was played."""
    import tempfile

    tb = tb or RfSampBufTxTB(name="tb", sim=Simulation())
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(root or tmp)
        write_scenario(base, samp_per_word=int(tb.samp_per_word), cmds=cmds)
        for part in (tb.cmd_drv, tb.sink, tb.resp_sink):
            part.root = base
        tb.sim.run_sim()
    return tb


def played_samples(tb: "RfSampBufTxTB") -> np.ndarray:
    """The samples the DAC actually played, as unsigned 16-bit words.

    Read off the RF sink — i.e. the far side of the converter — so the comparison covers packing,
    playout and the converter's own unpack, not just the buffer.
    """
    if not tb.sink.blocks:
        return np.zeros(0, dtype=np.uint64)
    flat = np.concatenate([np.asarray(b, dtype=np.float64).ravel() for b in tb.sink.blocks])
    full = float(1 << (SAMP_BW - 1))
    ints = np.rint(flat * full).astype(np.int64)
    return (ints % (1 << SAMP_BW)).astype(np.uint64)


def find_loaded_run(played: np.ndarray, start: int = PRIMED_AT, nsamp: int = 1024) -> int:
    """Length of the longest contiguous run of the loaded ramp inside *played*, from its first hit.

    **Neither backend may assume that played sample i is slot i.**  The DAC emits blocks as it gets
    them and zero-fills when it does not, so the offset between the play pointer and the sink's
    sample index depends on the startup transient — which differs between pysim (where the player
    runs ahead; see ``RfSampBufPlayer.dac_word_rate``) and RTL (where ``TREADY`` paces it).  What is
    the same in both is that the loaded samples must come out **in order and unbroken**, so that is
    what is measured.
    """
    want = ramp_samples(start + nsamp + 64)[start:start + nsamp]
    if played.size < 16:
        return 0
    for i in range(played.size - 16):
        if np.array_equal(played[i:i + 16], want[:16]):
            n = min(nsamp, played.size - i)
            k = 0
            while k < n and played[i + k] == want[k]:
                k += 1
            return k
    return 0


def responses(tb: "RfSampBufTxTB") -> list[tuple[int, int, int]]:
    """The ``(tid, status, nloaded)`` triples the DUT reported, deserialized through the schema."""
    if not tb.resp_sink.words:
        return []
    w = int(tb.rfdc.axis_bitwidth)
    flat = np.concatenate(tb.resp_sink.words).astype(np.uint64)
    n = TxResp.nwords_per_inst(w)
    out = []
    for i in range(0, flat.size, n):
        r = TxResp().deserialize(flat[i:i + n], word_bw=w)
        out.append((int(r.tid), int(r.status), int(r.nloaded)))
    return out
