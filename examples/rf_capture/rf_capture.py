"""rf_capture.py — ``RfSampBuf``, RX side: a capture buffer between the converter and the host.

``plans/adc_model.md`` staging item 3 (RX only) and ``plans/rtl_module.md`` S4.  The first RF block
that does something: samples stream in from the ADC continuously and are written to a circular
buffer; a command names a *window in sample index* and the captured samples come back out.

    s_in --> [ingress] --BramIF(write)--> T2pBram --BramIF(read)--> [capture] --> s_out
                 |                                                      ^            s_resp
                 +------------- progress channel (wr) ------------------+
    s_cmd --> [capture]

**Why it is two tasks and a memory rather than one task and an array.**  The two accessors are
concurrent by nature — the ADC never pauses and a capture may run for a long time — and Vitis has no
way to express a memory shared between two ``hls::task`` bodies: a local array becomes a
synchronizing PIPO whose handshake *stalls the writer*, which is the one thing a converter-facing
stage may never do.  So the buffer is hand-written Verilog beside the kernel
(:class:`~waveflow.hw.bram.T2pBram`), joined by a generated wrapper.  See
``docs/guide/interface/bram.md``.

**The never-stall law applies to the ingress only.**  It is written on
:class:`RfCapIngress` and deliberately *not* on :class:`RfCapture`: the capture may block for as long
as it likes, because nothing upstream of it loses data while it waits.  Copying the law onto the
capture would make it wrong — and the four command cases below exist precisely because it may wait.

**One sample per word, so a sample index is a word index.**  The converter is configured
``nbits=16, samp_per_word=1``, which makes ``RxCmd.start`` directly a buffer coordinate and keeps the
capture free of packing arithmetic.  Every port and channel of a Vitis top shares one width today, so
that choice also fixes the command word width at 16 bits and the sample counter with it: the counter
**wraps at 65536**, and every comparison against it is therefore a circular (signed-difference) one.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

import numpy as np

HERE = Path(__file__).resolve().parent

from waveflow.hw.bram import BramIF, BramIFMaster, T2pBram  # noqa: E402
from waveflow.hw.clock import Clock  # noqa: E402
from waveflow.hw.codegen_targets import COMPOSITE_KERNEL, SEQUENTIAL_XSI_TB  # noqa: E402
from waveflow.hw.dataschema import DataList, IntField  # noqa: E402
from waveflow.hw.hw_freerun import FreeRunMod  # noqa: E402
from waveflow.hw.hw_module import HwParam  # noqa: E402
from waveflow.hw.interface import StreamIF, StreamIFMaster, StreamIFSlave  # noqa: E402
from waveflow.hw.mem_stream import KernelTask  # noqa: E402
from waveflow.hw.rf_sample_if import RFSampIF  # noqa: E402
from waveflow.hw.synth import sim_only  # noqa: E402
from waveflow.simulation.rf_tb import RfDataSource  # noqa: E402
from waveflow.simulation.simobj import ProcessGen  # noqa: E402
from waveflow.simulation.simulation import Simulation  # noqa: E402
from waveflow.simulation.stream_tb import StreamDriver, StreamSink  # noqa: E402

from examples.rf_loopback.rfdc import Rfdc  # noqa: E402

# ---------------------------------------------------------------------------
# Geometry.  16-bit samples, one per word; a 1024-sample buffer is one RAMB18.
# ---------------------------------------------------------------------------

WORD_BW = 16
BUF_DEPTH = 1024

#: Samples of horizon given up to bound the progress channel's staleness — see
#: :attr:`RfCapture.horizon_margin`.  The usable horizon is ``BUF_DEPTH - HORIZON_MARGIN``.
HORIZON_MARGIN = 16

#: Response status codes, mirrored in ``rf_cap_capture_task.h``.  Two places, one encoding: the C++
#: side spells them as literals so the generated schema header cannot disagree about the value.
RF_CAP_OK = 0
RF_CAP_TOO_OLD = 1

Word16 = IntField.specialize(bitwidth=WORD_BW, signed=False)


class RxCmd(DataList):
    """One capture command: return the samples at indices ``[start, start + nsamp)``.

    The window is in **sample index** — the converter's own running count, not a buffer address —
    which is what lets a host ask for samples *around an event* it timestamped, and what makes three
    of the four cases (buffer / future / straddling) one question rather than three.
    """

    include_filename: ClassVar[str | None] = "rx_cmd.h"
    elements = {
        "tid":   {"schema": Word16, "description": "transaction id, echoed on the response"},
        "start": {"schema": Word16, "description": "first sample index of the window"},
        "nsamp": {"schema": Word16, "description": "samples to capture"},
    }


class RxResp(DataList):
    """One response per command — the **counted contract**.

    A capture that returned fewer samples than asked, or none at all, must say so in band: the
    alternative is a host that cannot tell "your window fell off the end of the buffer" from "the
    samples have not arrived yet", which is the difference between a bug and a wait.
    """

    include_filename: ClassVar[str | None] = "rx_resp.h"
    elements = {
        "tid":    {"schema": Word16, "description": "the command's transaction id"},
        "status": {"schema": Word16, "description": "0 = OK, 1 = too old (window fell off the horizon)"},
        "nsent":  {"schema": Word16, "description": "samples actually emitted"},
    }


SCHEMA_CLASSES = [RxCmd, RxResp]


def sdiff(a: int, b: int, bits: int = WORD_BW) -> int:
    """``a - b`` as a **signed circular difference** on a *bits*-wide counter.

    The pysim twin of the C++ ``(ap_int<W>)(a - b)``.  A plain ``a < b`` on a wrapping counter is
    wrong the first time it wraps, and wrong *silently*; this is exact as long as the two positions
    are within ``2**(bits-1)`` of each other, which a 1024-deep buffer guarantees.
    """
    half = 1 << (bits - 1)
    return ((int(a) - int(b) + half) % (1 << bits)) - half


# ---------------------------------------------------------------------------
# The two tasks
# ---------------------------------------------------------------------------

@dataclass
class RfCapIngress(FreeRunMod):
    """One sample off the converter port, one sample into the buffer, one progress update.

    **This task may never stall its input**, and it satisfies that *structurally* rather than by a
    sizing argument: it writes a BRAM port, and a BRAM port has no handshake to refuse it.  The
    rf_loopback ingress had to argue about FIFO depth; this one has nothing to size.

    The progress write is non-blocking on purpose.  A blocking write would stall the converter in
    order to deliver a number that is stale by the time it lands — see :class:`RfCapture` for what
    the resulting lag costs and how it is paid for.

    The body is hand-written (``src/rf_cap_ingress_task.h``); ``run_iter`` is the pysim twin and
    relays a whole **burst**, because a burst is pysim's quantum.  Identical at block granularity,
    which is the only granularity pysim resolves (``docs/guide/rf/fidelity.md``).
    """

    cpp_kernel_name: ClassVar[str | None] = "rf_cap_ingress"

    bitwidth: HwParam[int] = WORD_BW
    depth: HwParam[int] = BUF_DEPTH
    clk: Clock = field(default_factory=lambda: Clock(freq=300e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        w, d = int(self.bitwidth), int(self.depth)
        if d & (d - 1):
            raise ValueError(f"buffer depth must be a power of two (got {d}): the wrap is a mask")
        self.s_in = StreamIFSlave(sim=self.sim, name=f"{self.name}_s_in", bitwidth=w, has_tlast=True)
        self.buf_w = BramIFMaster(sim=self.sim, name=f"{self.name}_buf_w", bitwidth=w, depth=d,
                                  access="write")
        self.wr_out = StreamIFMaster(sim=self.sim, name=f"{self.name}_wr_out", bitwidth=w,
                                     has_tlast=True)
        for ep in (self.s_in, self.buf_w, self.wr_out):
            self.add_endpoint(ep)
        #: The write pointer in sample index, wrapping at ``2**bitwidth`` exactly as the RTL's does.
        self.wr = 0

    def kernel_task(self) -> KernelTask:
        return KernelTask("rf_cap_ingress_task", "rf_cap_ingress_task.h",
                          ("buf_w", "s_in", "wr_out"),
                          template_args=(int(self.bitwidth), int(self.depth)))

    def run_iter(self) -> ProcessGen[None]:
        words = yield from self.s_in.get()
        mask = int(self.depth) - 1
        wrap = 1 << int(self.bitwidth)
        for x in np.asarray(words).ravel():
            self.buf_w.mem_write(self.wr & mask, int(x))
            self.wr = (self.wr + 1) % wrap
        # `offer`, not `write`: the same non-blocking semantics as the RTL's write_nb, and for the
        # same reason.  What it drops is counted on the interface, and a nonzero count there is
        # normal rather than a fault -- only the newest position has meaning.
        yield from self.wr_out.offer(np.array([self.wr], dtype=np.uint64))


@dataclass
class RfCapture(FreeRunMod):
    """One ``RxCmd`` in, the named window out, one ``RxResp`` per command.

    **This task is allowed to block**, which is the whole reason the four cases collapse into one
    loop.  Nothing upstream of it loses data while it waits: the ingress keeps filling the buffer
    whatever this task is doing.

    ==============  =====================================  =========================================
    case            condition                              what happens
    ==============  =====================================  =========================================
    in the buffer   ``wr-N <= start, start+nsamp <= wr``    served straight out of the buffer
    in the future   ``start >= wr``                         waits per sample, then serves
    straddling      ``start < wr < start+nsamp``            pre-trigger from the buffer, then streams
    too old         ``start < wr - N``                      refused, counted, never a silent read
    ==============  =====================================  =========================================

    **The horizon is checked per sample, not per command.**  A long capture whose output is
    back-pressured can start legal and go stale mid-stream — valid when it was asked for, overwritten
    by the time it is read — so both bounds live inside the loop.

    **What the margin is for.**  :attr:`last_wr` is a *lower bound* on the true write pointer, because
    the progress channel drops rather than stalls.  Staleness makes the "already written?" test
    harder to pass (safe: this task merely waits longer) and the "not yet overwritten?" test *easier*
    to pass (unsafe: an overwritten sample could slip through).  So the usable horizon is declared as
    ``depth - horizon_margin``, and the margin is what makes the unsafe direction bounded rather than
    hoped-for.
    """

    cpp_kernel_name: ClassVar[str | None] = "rf_capture"

    bitwidth: HwParam[int] = WORD_BW
    depth: HwParam[int] = BUF_DEPTH
    #: Samples of horizon surrendered to bound the progress channel's lag.  It must exceed the number
    #: of samples the ingress can write between this task polling the channel and using the value —
    #: one ingress firing plus whatever the channel dropped while a sample was being written out.  At
    #: one sample per converter period and a capture loop of a few fabric cycles, that is a handful;
    #: 16 is a generous round number, and 1.6% of the buffer is a cheap price for a stated bound.
    horizon_margin: HwParam[int] = HORIZON_MARGIN
    clk: Clock = field(default_factory=lambda: Clock(freq=300e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        w, d = int(self.bitwidth), int(self.depth)
        self.wr_in = StreamIFSlave(sim=self.sim, name=f"{self.name}_wr_in", bitwidth=w,
                                   has_tlast=True)
        self.s_cmd = StreamIFSlave(sim=self.sim, name=f"{self.name}_s_cmd", bitwidth=w,
                                   has_tlast=True)
        self.s_out = StreamIFMaster(sim=self.sim, name=f"{self.name}_s_out", bitwidth=w,
                                    has_tlast=True)
        self.s_resp = StreamIFMaster(sim=self.sim, name=f"{self.name}_s_resp", bitwidth=w,
                                     has_tlast=True)
        self.buf_r = BramIFMaster(sim=self.sim, name=f"{self.name}_buf_r", bitwidth=w, depth=d,
                                  access="read")
        for ep in (self.wr_in, self.s_cmd, self.s_out, self.s_resp, self.buf_r):
            self.add_endpoint(ep)
        #: What this task last heard about the ingress's position — a LOWER bound, never an upper one.
        self.last_wr = 0
        #: Commands refused because their window had fallen off the horizon.  Not a diagnostic: it is
        #: the counted half of the contract, and a run in which it stays zero has not tested it.
        self.n_too_old = 0
        #: Commands that had to wait for at least one sample (the future / straddling cases).
        self.n_waited = 0

    def kernel_task(self) -> KernelTask:
        return KernelTask("rf_cap_capture_task", "rf_cap_capture_task.h",
                          ("buf_r", "wr_in", "s_cmd", "s_out", "s_resp"),
                          template_args=(int(self.bitwidth), int(self.depth),
                                         int(self.horizon_margin)))

    @sim_only
    def count_too_old(self) -> None:
        """Tally a refused command — instrumentation, and marked as such."""
        self.n_too_old += 1

    @sim_only
    def count_waited(self) -> None:
        """Tally a command that had to wait for the converter."""
        self.n_waited += 1

    def run_iter(self) -> ProcessGen[None]:
        w = int(self.bitwidth)
        mask = int(self.depth) - 1
        wrap = 1 << w
        usable = int(self.depth) - int(self.horizon_margin)

        cmd = yield from self.s_cmd.get(RxCmd)
        idx = int(cmd.start)
        sent = 0
        status = RF_CAP_OK
        waited = False

        for _ in range(int(cmd.nsamp)):
            # 1. Wait until sample `idx` has been written.  Poll first (take the newest position the
            #    channel is holding), and only then block -- there is nothing to do until the ingress
            #    advances, and blocking costs one event instead of one per fabric cycle.
            while True:
                got = yield from self.wr_in.get_nb()
                if got is not None:
                    self.last_wr = int(np.asarray(got).ravel()[-1])
                if sdiff(idx, self.last_wr, w) < 0:
                    break
                waited = True
                got = yield from self.wr_in.get()
                self.last_wr = int(np.asarray(got).ravel()[-1])

            # 2. Horizon, per sample.
            if sdiff(self.last_wr, idx, w) > usable:
                status = RF_CAP_TOO_OLD
                self.count_too_old()
                break

            val = self.buf_r.mem_read(idx & mask)
            yield from self.s_out.write(np.array([val], dtype=np.uint64))
            sent += 1
            idx = (idx + 1) % wrap

        if waited:
            self.count_waited()
        resp = RxResp(tid=int(cmd.tid), status=int(status), nsent=int(sent))
        yield from self.s_resp.write(resp)


# ---------------------------------------------------------------------------
# The composite: two tasks, one channel, one memory beside the kernel
# ---------------------------------------------------------------------------

@dataclass
class RfSampBufRx(FreeRunMod):
    """The RX capture buffer as one design scope: ingress + capture + the memory between them.

    The registrations are the design:

    ===========================  =============================================================
    ``add_comp(ingress/capture)`` the two ``hls::task``\\ s inside the generated kernel
    ``add_if(wr_if)``             the progress channel -> an ``hls::stream`` **depth 1**
    ``add_rtl_mod(mem)``          the buffer, realized as hand-written Verilog beside the kernel
    ``add_rtl_if(...)``           wrapper wires -> the tasks' memory ports stay boundary ports
    ===========================  =============================================================

    **The progress channel is depth 1 on purpose.**  It carries a running position, so only the
    newest value means anything and a deeper queue would only serve older ones.  Combined with the
    non-blocking write on one end and a non-blocking poll on the other, "the channel is full" simply
    means "the capture already knows roughly where we are".
    """

    cpp_kernel_name: ClassVar[str | None] = "rf_samp_buf_rx"
    potential_targets: ClassVar[frozenset[str]] = frozenset({COMPOSITE_KERNEL})

    bitwidth: HwParam[int] = WORD_BW
    depth: HwParam[int] = BUF_DEPTH
    horizon_margin: HwParam[int] = HORIZON_MARGIN
    clk: Clock = field(default_factory=lambda: Clock(freq=300e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        w, d = int(self.bitwidth), int(self.depth)
        self.ingress = RfCapIngress(sim=self.sim, name=f"{self.name}_ingress", bitwidth=w, depth=d,
                                    clk=self.clk)
        self.capture = RfCapture(sim=self.sim, name=f"{self.name}_capture", bitwidth=w, depth=d,
                                 horizon_margin=int(self.horizon_margin), clk=self.clk)
        self.add_comp(self.ingress)
        self.add_comp(self.capture)

        wr_if = StreamIF(name=f"{self.name}_wr_if", sim=self.sim, clk=self.clk, bitwidth=w, depth=1)
        wr_if.bind(ep_name="master", endpoint=self.ingress.wr_out)
        wr_if.bind(ep_name="slave", endpoint=self.capture.wr_in)
        self.add_if(wr_if)

        # `mem`, not `buf`: the attribute name becomes the Verilog instance name and `buf` is a
        # primitive gate (the wrapper emitter refuses it by name).
        self.mem = T2pBram(sim=self.sim, name=f"{self.name}_mem", dwidth=w, depth=d)
        self.add_rtl_mod(self.mem)
        w_if = BramIF(name=f"{self.name}_bufw_if", sim=self.sim)
        w_if.bind(ep_name="master", endpoint=self.ingress.buf_w)
        w_if.bind(ep_name="slave", endpoint=self.mem.wr_port)
        self.add_rtl_if(w_if)
        r_if = BramIF(name=f"{self.name}_bufr_if", sim=self.sim)
        r_if.bind(ep_name="master", endpoint=self.capture.buf_r)
        r_if.bind(ep_name="slave", endpoint=self.mem.rd_port)
        self.add_rtl_if(r_if)

        #: ``add_comp`` x ``add_endpoint`` order, with the progress endpoints removed.  The two
        #: ``buf_*`` entries are ports of the KERNEL, joined to the memory inside the wrapper.
        self.boundary = ["s_in", "buf_w", "s_cmd", "s_out", "s_resp", "buf_r"]

        # Convenience refs for the testbenches — the boundary endpoints live on the children.
        self.s_in = self.ingress.s_in
        self.s_cmd = self.capture.s_cmd
        self.s_out = self.capture.s_out
        self.s_resp = self.capture.s_resp

    @property
    def n_too_old(self) -> int:
        """Commands refused because their window had fallen off the horizon."""
        return int(self.capture.n_too_old)

    @property
    def n_waited(self) -> int:
        """Commands that had to wait for the converter at least once."""
        return int(self.capture.n_waited)


# ---------------------------------------------------------------------------
# The testbench graph, and the scenario that exercises all four cases
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
            resp.append((tid, RF_CAP_TOO_OLD, 0))
            continue
        words.extend(int(ramp[start + i]) for i in range(nsamp))
        resp.append((tid, RF_CAP_OK, nsamp))
    return np.array(words, dtype=np.uint64), resp


def command_bursts(cmds=GATE_COMMANDS) -> list[np.ndarray]:
    """The command stream as one burst per command — the bytes both backends play.

    Serialized through the schema, never by hand: ``RxCmd`` decides how its three fields land in
    16-bit words, and the C++ ``read_stream<16>`` is generated from that same schema.
    """
    return [np.asarray(RxCmd(tid=t, start=s, nsamp=n).serialize(word_bw=WORD_BW), dtype=np.uint64)
            for t, s, n in cmds]


@dataclass
class RfCaptureTB(FreeRunMod):
    """The graph: an ADC filling the buffer, a host issuing commands, two sinks collecting.

    ::

        RfDataSource --RFSampIF--> Rfdc.rx_rf | Rfdc.rx_stream --StreamIF--> RfSampBufRx
        StreamDriver --StreamIF--> s_cmd                          s_out --StreamIF--> StreamSink
                                                                  s_resp --StreamIF--> StreamSink

    **The converter is really here**, not a stand-in driver, because the one thing this design exists
    to satisfy is condition 3 of the fidelity contract — the DUT never stalls its input — and only a
    converter can fail to be back-pressured.  The tile is ADC-only (``n_tx=0``): a capture design has
    no transmitter, and wiring a fake DAC in to satisfy the model would add a metronome nothing feeds.

    ``samp_per_word=1`` so one AXIS word is one sample and ``RxCmd.start`` is directly a buffer
    coordinate.
    """

    potential_targets: ClassVar[frozenset[str]] = frozenset({SEQUENTIAL_XSI_TB})

    n_blk: int = XSI_NBLK
    blksize: int = XSI_BLKSIZE
    samp_rate: float = 256e6
    axis_freq: float = 300e6
    nbits: int = WORD_BW
    depth: int = BUF_DEPTH
    horizon_margin: int = HORIZON_MARGIN
    #: Fixed run bound for the generated XSI main — a testbench constant, not a latency.
    n_cycles: int = 40000
    axis_clk: Clock = field(default_factory=lambda: Clock(freq=300e6))

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


def run_pysim(root=None, tb: "RfCaptureTB | None" = None) -> "RfCaptureTB":
    """Run the graph in SimPy and return the testbench, its sinks holding what was captured."""
    import tempfile

    tb = tb or RfCaptureTB(name="tb", sim=Simulation())
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(root or tmp)
        write_scenario(base)
        for part in (tb.source, tb.cmd_drv, tb.out_sink, tb.resp_sink):
            part.root = base
        tb.sim.run_sim()
    return tb


def captured_words(tb: "RfCaptureTB") -> np.ndarray:
    """The captured samples, as unsigned 16-bit words."""
    if not tb.out_sink.words:
        return np.zeros(0, dtype=np.uint64)
    return np.concatenate(tb.out_sink.words).astype(np.uint64)


def responses(tb: "RfCaptureTB") -> list[tuple[int, int, int]]:
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
