"""rf_samp_buf_rx.py — the RX capture demo for :class:`~waveflow.hw.rf_samp_buf.RfSampBufRx`.

``plans/adc_model.md`` staging item 3 (RX only) and ``plans/rtl_module.md`` S4.  The **module** is
framework and lives in :mod:`waveflow.hw.rf_samp_buf`; what is left here is what an example should
be — a graph that wires it to a real converter, a scenario that exercises every case it claims to
handle, and a predicted golden:

    RfDataSource --RFSampIF--> Rfdc.rx_rf | Rfdc.rx_stream --StreamIF--> RfSampBufRx
    StreamDriver --StreamIF--> s_cmd                          s_out --StreamIF--> StreamSink
                                                              s_resp --StreamIF--> StreamSink

**The converter is really here**, not a stand-in driver, because the one thing this design exists to
satisfy is condition 3 of the fidelity contract — the DUT never stalls its input — and only a
converter can fail to be back-pressured.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

import numpy as np

HERE = Path(__file__).resolve().parent

from waveflow.hw.clock import Clock  # noqa: E402
from waveflow.hw.codegen_targets import SEQUENTIAL_XSI_TB  # noqa: E402
from waveflow.hw.hw_freerun import FreeRunMod  # noqa: E402
from waveflow.hw.interface import StreamIF  # noqa: E402
from waveflow.hw.rf_samp_buf import (  # noqa: E402
    BUF_DEPTH,
    HORIZON_MARGIN,
    RF_SAMP_BUF_OK,
    RF_SAMP_BUF_TOO_OLD,
    SCHEMA_CLASSES,
    WORD_BW,
    RfSampBufIngress,
    RfSampBufRx,
    RxCmd,
    RxResp,
    sdiff,
)
from waveflow.hw.rf_sample_if import RFSampIF  # noqa: E402
from waveflow.simulation.rf_tb import RfDataSource  # noqa: E402
from waveflow.simulation.simulation import Simulation  # noqa: E402
from waveflow.simulation.stream_tb import StreamDriver, StreamSink  # noqa: E402

from examples.rf_loopback.rfdc import Rfdc  # noqa: E402

# Re-exported so a reader of this example does not have to know which names are framework and which
# are the scenario's -- the import above is the statement about where each one lives.
__all__ = [
    "BUF_DEPTH", "HORIZON_MARGIN", "RF_SAMP_BUF_OK", "RF_SAMP_BUF_TOO_OLD", "SCHEMA_CLASSES",
    "WORD_BW", "RfSampBufIngress", "RfSampBufRx", "RxCmd", "RxResp", "sdiff",
    "GATE_COMMANDS", "SAMP_BASE", "XSI_BLKSIZE", "XSI_NBLK", "XSI_NSAMP", "RfSampBufRxTB",
    "captured_words", "command_bursts", "expected_capture", "ramp_samples", "responses",
    "run_pysim", "write_scenario",
]

# ---------------------------------------------------------------------------
# The scenario
# ---------------------------------------------------------------------------

#: The gate scenario.  16 RF blocks of 256 samples = 4096 samples, against a 1024-sample buffer, so
#: the buffer wraps three times and "too old" is reachable without contriving anything.
XSI_NBLK = 16
XSI_BLKSIZE = 256
XSI_NSAMP = XSI_NBLK * XSI_BLKSIZE

#: Sample *values* are a ramp, ``SAMP_BASE + i``, so every captured word identifies the index it came
#: from.  A constant would pass a capture that returned the wrong window, which is the whole failure
#: mode a capture buffer has.
SAMP_BASE = 1000


def ramp_samples(nsamp: int = XSI_NSAMP, base: int = SAMP_BASE) -> np.ndarray:
    """The sample stream both backends play: ``base + i`` at index ``i``."""
    return ((np.arange(int(nsamp), dtype=np.int64) + int(base)) % (1 << WORD_BW)).astype(np.uint64)


#: The four commands, and **why each one is the case it claims to be**.  Determinism comes from the
#: ORDER, not from timing: the capture serves commands one at a time and blocks per sample, so each
#: command's case is implied by the completion of the one before it.
#:
#: 1. ``tid=1  start=3800 nsamp=8``   — **future**.  Issued at t=0 when ``wr == 0``, and 3800 samples
#:    take 15 block periods to arrive, so this must wait.
#: 2. ``tid=2  start=3600 nsamp=8``   — **in the buffer**.  Command 1 completing proves ``wr >= 3808``;
#:    the run is 4096 samples so ``wr <= 4096``, and ``3600 >= 4096 - 1008``.  Both bounds hold for
#:    every possible ``wr``, so this cannot be anything else.
#: 3. ``tid=3  start=3800 nsamp=100`` — **straddling**.  ``wr`` is 3840 when command 1 completes (the
#:    ADC advances it 256 at a time), so the window [3800, 3900) has its head in the buffer and its
#:    tail in the future.  The same window as command 1, served a different way — which is the point.
#: 4. ``tid=4  start=0    nsamp=4``   — **too old**.  ``wr >= 3840`` by now and ``3840 - 0 > 1008``.
#:
#: Command 3 is the one a trigger actually wants: "100 samples around the event", where the event is
#: at the edge of what has arrived.
GATE_COMMANDS = (
    (1, 3800, 8),
    (2, 3600, 8),
    (3, 3800, 100),
    (4, 0, 4),
)


def expected_capture() -> tuple[np.ndarray, list[tuple[int, int, int]]]:
    """The **predicted** result: the words the sink must see, and one ``(tid, status, nsent)`` per
    command.

    Derived from the command semantics alone — a captured sample is ``SAMP_BASE + idx`` whatever the
    timing did — so this is a prediction, not a transcription of a run.
    """
    words: list[int] = []
    resp: list[tuple[int, int, int]] = []
    ramp = ramp_samples()
    for tid, start, nsamp in GATE_COMMANDS:
        if start < XSI_NSAMP - (BUF_DEPTH - HORIZON_MARGIN):
            # The window is older than the horizon by the time it is examined: nothing is emitted.
            resp.append((tid, RF_SAMP_BUF_TOO_OLD, 0))
            continue
        words.extend(int(ramp[start + i]) for i in range(nsamp))
        resp.append((tid, RF_SAMP_BUF_OK, nsamp))
    return np.array(words, dtype=np.uint64), resp


def command_bursts(cmds=GATE_COMMANDS) -> list[np.ndarray]:
    """The command stream as one burst per command — the bytes both backends play.

    Serialized through the schema, never by hand: ``RxCmd`` decides how its three fields land in
    16-bit words, and the C++ ``read_stream<16>`` is generated from that same schema.
    """
    return [np.asarray(RxCmd(tid=t, start=s, nsamp=n).serialize(word_bw=WORD_BW), dtype=np.uint64)
            for t, s, n in cmds]


# ---------------------------------------------------------------------------
# The testbench graph
# ---------------------------------------------------------------------------

@dataclass
class RfSampBufRxTB(FreeRunMod):
    """An ADC filling the buffer, a host issuing commands, two sinks collecting.

    The tile is ADC-only (``n_tx=0``): a capture design has no transmitter, and wiring a fake DAC in
    to satisfy the model would add a metronome nothing feeds.

    ``samp_per_word=1`` so one AXIS word is one sample and ``RxCmd.start`` is directly a buffer
    coordinate.
    """

    potential_targets: ClassVar[frozenset[str]] = frozenset({SEQUENTIAL_XSI_TB})

    n_blk: int = XSI_NBLK
    blksize: int = XSI_BLKSIZE
    #: 64 MSPS on a 300 MHz fabric — 0.213 samples per cycle against an ingress that absorbs 0.5.
    #: NOT a free parameter: see :meth:`check_rate`, which refuses a rate this design cannot take.
    samp_rate: float = 64e6
    axis_freq: float = 300e6
    nbits: int = WORD_BW
    depth: int = BUF_DEPTH
    horizon_margin: int = HORIZON_MARGIN
    #: Fixed run bound for the generated XSI main — a testbench constant, not a latency.
    n_cycles: int = 40000
    axis_clk: Clock = field(default_factory=lambda: Clock(freq=300e6))

    def check_rate(self) -> float:
        """Refuse a sample rate the ingress cannot absorb, and return the utilisation.

        The arithmetic and the message live on the module
        (:meth:`~waveflow.hw.rf_samp_buf.RfSampBufRx.check_rate`) because a module's throughput is
        part of its interface contract; what a testbench owns is the **pairing** — this converter
        with this design — so it is called here with both halves.
        """
        return self.dut.check_rate(float(self.samp_rate), float(self.axis_freq),
                                   int(self.rfdc.samp_per_word))

    def __post_init__(self) -> None:
        super().__post_init__()
        self.axis_clk = Clock(name=f"{self.name}_axis_clk", freq=float(self.axis_freq))
        self.samp_clk = Clock(name=f"{self.name}_samp_clk", freq=float(self.samp_rate))
        self.blk_period = int(self.blksize) / float(self.samp_rate)

        self.rfdc = Rfdc(name=f"{self.name}_rfdc", sim=self.sim, n_rx=1, n_tx=0,
                         nbits=int(self.nbits), samp_per_word=1)
        w = self.rfdc.axis_bitwidth
        self.dut = RfSampBufRx(name=f"{self.name}_dut", sim=self.sim, bitwidth=w,
                               depth=int(self.depth), horizon_margin=int(self.horizon_margin),
                               clk=self.axis_clk)
        #: Fraction of the ingress's capacity this scenario asks for — checked, not assumed.
        self.rate_util = self.check_rate()
        self.source = RfDataSource(name=f"{self.name}_src", sim=self.sim, in_bundle="vectors/rf_in")
        self.cmd_drv = StreamDriver(sim=self.sim, name=f"{self.name}_cmd_drv", bitwidth=w,
                                    in_bundle="vectors/cmd", has_tlast=True)
        self.out_sink = StreamSink(sim=self.sim, name=f"{self.name}_out_snk", bitwidth=w,
                                   out_bundle="vectors/out", has_tlast=True)
        self.resp_sink = StreamSink(sim=self.sim, name=f"{self.name}_resp_snk", bitwidth=w,
                                    out_bundle="vectors/resp", has_tlast=True)
        for c in (self.dut, self.rfdc, self.source, self.cmd_drv, self.out_sink, self.resp_sink):
            self.add_comp(c)

        # --- the RF domain: one interface, one metronome (there is no DAC) --------------------
        self.adc_if = RFSampIF(name=f"{self.name}_adc_if", sim=self.sim, samp_clk=self.samp_clk,
                               n_ch=1, blksize=int(self.blksize), n_blk=int(self.n_blk))
        self.adc_if.bind("tx", self.source.rf_ep)
        self.adc_if.bind("rx", self.rfdc.rx_rf)
        self.add_if(self.adc_if)

        # --- the PL domain --------------------------------------------------------------------
        # No depth overrides: these become the DUT's top-level AXIS ports, and a top-level argument
        # cannot carry a FIFO depth (Vitis ignores the pragma).  The elasticity that matters is
        # inside the design -- and here it is the BRAM itself.
        self.adc_axis = StreamIF(name=f"{self.name}_adc_axis", sim=self.sim, clk=self.axis_clk,
                                 bitwidth=w)
        self.adc_axis.bind("master", self.rfdc.rx_stream)
        self.adc_axis.bind("slave", self.dut.s_in)
        self.add_if(self.adc_axis)

        cmd_axis = StreamIF(name=f"{self.name}_cmd_axis", sim=self.sim, clk=self.axis_clk,
                            bitwidth=w)
        cmd_axis.bind("master", self.cmd_drv.stream_ep)
        cmd_axis.bind("slave", self.dut.s_cmd)
        self.add_if(cmd_axis)

        out_axis = StreamIF(name=f"{self.name}_out_axis", sim=self.sim, clk=self.axis_clk,
                            bitwidth=w)
        out_axis.bind("master", self.dut.s_out)
        out_axis.bind("slave", self.out_sink.stream_ep)
        self.add_if(out_axis)

        resp_axis = StreamIF(name=f"{self.name}_resp_axis", sim=self.sim, clk=self.axis_clk,
                             bitwidth=w)
        resp_axis.bind("master", self.dut.s_resp)
        resp_axis.bind("slave", self.resp_sink.stream_ep)
        self.add_if(resp_axis)


def write_scenario(root) -> None:
    """Materialize ``<root>/vectors/rf_in`` and ``.../cmd`` — what BOTH backends play.

    One writer, so the RTL run and the pysim golden cannot start from different bytes.
    """
    from waveflow.simulation.rf_tb import write_rf_bundle
    from waveflow.utils.burst_io import write_burst_bundle

    root = Path(root)
    ramp = ramp_samples()
    # The RF source plays real-valued blocks; the converter quantizes them to `nbits`.  Sending the
    # ramp as an INTEGER amplitude scaled into [-1, 1) means the quantizer round-trips it exactly,
    # so what the buffer holds is the ramp itself and a captured word names its own index.
    full = float(1 << (WORD_BW - 1))
    blocks = [np.asarray(_signed(ramp[i * XSI_BLKSIZE:(i + 1) * XSI_BLKSIZE]), dtype=float).reshape(1, -1)
              / full for i in range(XSI_NBLK)]
    write_rf_bundle(blocks, root / "vectors" / "rf_in")
    write_burst_bundle(command_bursts(), root / "vectors" / "cmd")


def _signed(words: np.ndarray) -> np.ndarray:
    """Reinterpret unsigned 16-bit words as the signed sample values the converter carries."""
    w = np.asarray(words, dtype=np.int64)
    return np.where(w >= (1 << (WORD_BW - 1)), w - (1 << WORD_BW), w)


def run_pysim(root=None, tb: "RfSampBufRxTB | None" = None) -> "RfSampBufRxTB":
    """Run the graph in SimPy and return the testbench, its sinks holding what was captured."""
    import tempfile

    tb = tb or RfSampBufRxTB(name="tb", sim=Simulation())
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(root or tmp)
        write_scenario(base)
        for part in (tb.source, tb.cmd_drv, tb.out_sink, tb.resp_sink):
            part.root = base
        tb.sim.run_sim()
    return tb


def captured_words(tb: "RfSampBufRxTB") -> np.ndarray:
    """The captured samples, as unsigned 16-bit words."""
    if not tb.out_sink.words:
        return np.zeros(0, dtype=np.uint64)
    return np.concatenate(tb.out_sink.words).astype(np.uint64)


def responses(tb: "RfSampBufRxTB") -> list[tuple[int, int, int]]:
    """The ``(tid, status, nsent)`` triples the DUT reported, deserialized through the schema."""
    if not tb.resp_sink.words:
        return []
    flat = np.concatenate(tb.resp_sink.words).astype(np.uint64)
    n = RxResp.nwords_per_inst(WORD_BW)
    out = []
    for i in range(0, flat.size, n):
        r = RxResp().deserialize(flat[i:i + n], word_bw=WORD_BW)
        out.append((int(r.tid), int(r.status), int(r.nsent)))
    return out
