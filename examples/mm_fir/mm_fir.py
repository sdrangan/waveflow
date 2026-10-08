"""mm_fir.py — a free-running FIR whose taps are registers and whose samples are a queue.

The witness example of ``plans/mm_slave_adaptor.md``.  The kernel is an ordinary stream-only
``FreeRunMod``; every memory-mapped view it has is an adaptor module in front of it, reached by a
host through one crossbar:

====================  =========  ===========================================================
view                  base       kernel side
====================  =========  ===========================================================
register bank         0x0000     ``s_cfg`` <- one :class:`FirCfg` per commit;
                                 ``m_status`` -> :class:`FirStatus` (latest-value)
queue in              0x1000     ``s_in``  <- ``FirCmdHdr | x[0] .. x[nsamp-1]`` per packet
queue out             0x2000     ``m_out`` -> one output word per sample
queue out (response)  0x3000     ``m_resp`` -> one :class:`FirRespHdr` per packet
====================  =========  ===========================================================

(The same 1x3 shape as the Stage 2 RTL gate, ``tests/build/test_mm_regbank_xsi.py``.)

**The stream_inband pattern, with a config id** (``plans/mm_fir_cfg_seq.md``).  The host names every
config it commits -- :attr:`FirCfg.cfg_id`, ``1, 2, ...`` -- and every sample packet is preceded in-band
by a :class:`FirCmdHdr`: its sample count, its ``tx_id`` and the ``cfg_id`` of the config it needs.
Per packet the kernel reads the header, takes configs from ``s_cfg`` until the one in force has that
id -- **waiting** if it has not arrived -- then reads the samples, filters them and writes the results.
The id is carried in the messages rather than counted at both ends, so a host that lost its place
needs nothing from the kernel: it commits a config with a new id and tags its packets with it.

**Cross-view order is carried in the messages** (the slave page's ordering statement 2).  A config and
and the samples travel on different streams, so either could reach the kernel first.  The config
id makes that harmless: a packet cannot use a config older than the one it names (the kernel waits for
it), nor a newer one (a config nobody has asked for stays in its stream).  So the host commits a config
and sends the packets that need it, in either order, and never has to ask whether the config arrived.

**And the host can check it.**  After each packet the kernel writes a :class:`FirRespHdr` to a second
queue out, the response FIFO: the packet's ``tx_id`` and the ``cfg_id`` it actually filtered with.
The host compares each response with the config it meant the packet to use.  The wait makes a wrong
config impossible from the kernel's side; the echo catches a host that asked for the wrong one.

Numbers: samples are int16 (:data:`S16`), packed by the serializer four to a 64-bit word; taps are
int16; outputs are the exact integer convolution, int64 (:data:`S64`), one per word.  Nothing on either
side packs a word by hand: Python uses the schema's serializer, the HLS body the generated
``int16_array_utils`` / ``int64_array_utils`` lane routines.  No rounding anywhere, so the numpy
golden :func:`fir_golden` is bit-exact by construction.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar

import numpy as np

from waveflow.hw.arrayutils import array
from waveflow.hw.clock import Clock
from waveflow.hw.dataschema import DataArray, DataList, IntField
from waveflow.hw.hw_freerun import FreeRunMod
from waveflow.hw.hw_module import DynParam, HwModule, HwParam
from waveflow.hw.interface import StreamIF, StreamIFMaster, StreamIFSlave
from waveflow.hw.irq import IrqIF, IrqIFSink
from waveflow.hw.memif import AXIMMCrossBarIF, MMIFMaster, assign_address_ranges
from waveflow.hw.mm_device import QueueIn, QueueOut, RegBank, build_mm_device
from waveflow.hw.mm_host import (
    BoundMemSlaveAdaptor,
    LatestValueIF,
    LatestValueIFSlave,
    MemSlaveLayout,
    write_trace,
)
from waveflow.simulation.simulation import Simulation

DW = 64
NTAP_MAX = 16
QDEPTH = 64

S16 = IntField.specialize(bitwidth=16, signed=True, include_dir="include")
U32 = IntField.specialize(bitwidth=32, signed=False)
#: One result on ``m_out``: the exact sum, int64.
S64 = IntField.specialize(bitwidth=64, signed=True, include_dir="include")
Taps = DataArray.specialize(S16, max_shape=(NTAP_MAX,))


U16 = IntField.specialize(bitwidth=16, signed=False)


class FirCmdHdr(DataList):
    """The in-band header in front of every sample packet on ``s_in`` -- one 64-bit word."""

    elements = {
        "nsamp": {"schema": U32, "description": "samples in this packet"},
        "tx_id": {"schema": U16, "description": "the host's packet id, echoed in the response"},
        "cfg_id": {"schema": U16, "description": "the id of the config this packet needs"},
    }


class FirRespHdr(DataList):
    """The kernel's response to one packet, on the response FIFO -- one 64-bit word."""

    elements = {
        "nsamp": {"schema": U32, "description": "samples filtered in this packet"},
        "tx_id": {"schema": U16, "description": "echo of the packet's tx_id"},
        "cfg_id": {"schema": U16, "description": "the id of the config the packet was filtered with"},
    }


class FirCfg(DataList):
    """One configuration: ``ntaps`` taps from ``coeffs``.

    ``coeffs`` comes FIRST on purpose.  At 64 bits Python packs a DataList densely, while the
    generated C++ starts an array on a fresh word (``plans/stream_array_alignment.md``, not fixed
    yet).  Sixteen int16 taps are exactly four 64-bit words, so with the array first both layouts
    agree; with ``ntaps`` first the C++ read the taps 32 bits late.
    """

    elements = {
        "coeffs": {"schema": Taps, "description": "tap k multiplies x[n-k]"},
        "ntaps": {"schema": U32, "description": "active taps (<= NTAP_MAX)"},
        "cfg_id": {"schema": U16,
                   "description": "the host's name for this config, 1..65535 (0 = no config yet); "
                                  "packets ask for it by this id"},
    }


class FirStatus(DataList):
    """What the kernel publishes after every packet (latest value wins)."""

    elements = {
        "nsamp": {"schema": U32, "description": "samples filtered so far"},
        "cfg_id": {"schema": U16, "description": "the id of the config in force (0 = none yet)"},
        "ncfg": {"schema": U16, "description": "configs taken so far"},
    }


#: Where the FIR's memory-mapped side is placed on the bus -- the per-SYSTEM half of the address map
#: (``plans/bus_address_map.md``).  The view addresses below are this plus the per-TYPE layout.
MM_BASE = 0x0000

#: The host's bus master: one read and one write in flight at a time, each direction in issue order.
#: AXI's read and write channels are independent and AMD's crossbar routes them in parallel, so a host
#: whose writer and reader run concurrently overlaps them -- but each process waits for its own
#: transaction before the next.  ONE setting for both backends: pysim's ``MMIFMaster.max_outstanding``
#: and the XSI testbench's ``AxiMmMaster(..., overlap_rw=true)`` (``mm_fir_xsi``) both read it.
HOST_MAX_OUTSTANDING = 1

#: The host's own pacing: cycles between a process asking for its next transaction and the master
#: presenting it.  2 is what the XSI testbench's host does (an endpoint sees its previous transaction
#: finish, then ``AxiMmMaster`` presents the next), measured by lining up both backends' bus
#: operations.  It models the TESTBENCH host; a real host's number is its own.
HOST_ISSUE_CYCLES = 2

#: AMD's ``axi_crossbar`` as ``AxiXbarConfig`` generates it, at 100 MHz, measured at RTL: a request
#: takes 4 cycles, 2 of them travelling to the slave -- overlapped with whatever the slave is serving.
#: (``plans/mm_adaptor_host_endpoints.md``, the timing probe.)  A property of the interconnect, so it
#: belongs in the platform model; it is here until that exists.
XBAR_LATENCY, XBAR_TRAVEL = 4.0, 2.0
#: Depth of the response FIFO: one word per packet, so a handful of packets in flight.
RDEPTH = 16


# ---------------------------------------------------------------------------
# Golden
# ---------------------------------------------------------------------------

def fir_golden(x, cfgs) -> np.ndarray:
    """Exact FIR over the whole stream: sample *n* uses the latest config with ``apply_at <= n``.

    The filter history is continuous across a switch (only the taps change), and samples before the
    first config see all-zero taps.  *cfgs* is ``[(apply_at, taps), ...]``.

    Each config's taps are applied to the WHOLE input with one ``np.convolve``, and the config keeps
    the samples it is in force for.  ``np.convolve`` on ``int64`` is integer arithmetic, so this is
    exact with no rounding: int16 samples times int16 taps, summed over at most 16 taps, need 37 bits.
    """
    x = np.asarray(x, dtype=np.int64)
    y = np.zeros(len(x), dtype=np.int64)
    order = sorted(cfgs, key=lambda c: c[0])        # stable: of two configs at one sample, the later wins
    for i, (at, taps) in enumerate(order):
        end = min(order[i + 1][0] if i + 1 < len(order) else len(x), len(x))
        taps = np.asarray(taps, dtype=np.int64)
        if end > at and len(taps):
            y[at:end] = np.convolve(x, taps)[at:end]
    return y


def make_cfg(taps, cfg_id: int = 1) -> FirCfg:
    taps = list(taps)
    if not 1 <= len(taps) <= NTAP_MAX:
        raise ValueError(f"1..{NTAP_MAX} taps, got {len(taps)}")
    if not 1 <= int(cfg_id) <= 0xFFFF:
        raise ValueError(f"cfg_id must be 1..65535 (0 means no config), got {cfg_id}")
    coeffs = np.zeros(NTAP_MAX, dtype=np.int64)
    coeffs[:len(taps)] = taps
    return FirCfg(ntaps=len(taps), coeffs=coeffs, cfg_id=int(cfg_id))


# ---------------------------------------------------------------------------
# The kernel
# ---------------------------------------------------------------------------

@dataclass
class MmFir(FreeRunMod):
    """The FIR kernel: streams only.  One firing = one packet: its header, the config it needs, its
    samples."""

    cpp_kernel_name: ClassVar[str | None] = "mm_fir"
    cpp_namespace: ClassVar[str | None] = "mm_fir_impl"

    #: The kernel's memory-mapped views, in address order (``plans/bus_address_map.md`` D1): which of
    #: its stream ports a bus master reaches, and as what.  This is the TYPE's address layout --
    #: ``MemSlaveLayout.of(MmFir)`` -- shared by every instance; ``build_mm_device`` builds an
    #: instance's views from it.  The kernel itself stays streams only.
    mm_views: ClassVar[tuple] = (
        RegBank("regs", cfg_port="s_cfg", status_port="m_status",
                cfg_type=FirCfg, status_type=FirStatus),
        QueueIn("qin", port="s_in", depth=QDEPTH),
        QueueOut("qout", port="m_out", depth=QDEPTH),
        QueueOut("qresp", port="m_resp", depth=RDEPTH),
    )

    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))
    ntap_max: HwParam[int] = NTAP_MAX
    #: Timing of the HLS body -- one packet per firing, the sample loop pipelined at II=1 -- MEASURED at
    #: RTL with mm_fir_xsi's handshake probes (packet 2: header read at 88, samples from 92, results
    #: 101..116, status and response at 126, next header at 128 -- 40 cycles for 16 samples):
    #:
    #: * ``hdr_cycles``: from the header to the first sample the loop can take (the header, the config
    #:   check, the loop's entry) -- the samples cannot start before their own header is handled;
    #: * ``proc_ii`` / ``proc_latency``: one sample per cycle; a result leaves this long after its sample;
    #: * ``tail_cycles``: from the last result to the status and response (the loop's exit, the two
    #:   messages);
    #: * ``restart_cycles``: from the response to the next firing's header.
    proc_ii: int = 1
    proc_latency: int = 9
    hdr_cycles: int = 4
    tail_cycles: int = 10
    restart_cycles: int = 2

    def kernel_task(self):
        """The hand-written HLS body, ``include/mm_fir_task.h`` -- the twin of :meth:`run_iter`."""
        from waveflow.hw.mem_stream import KernelTask
        return KernelTask("mm_fir_task", "mm_fir_task.h",
                          ("s_cfg", "s_in", "m_out", "m_resp", "m_status"), template_args=(DW,))

    def __post_init__(self) -> None:
        super().__post_init__()
        self.s_cfg = StreamIFSlave(name=f"{self.name}_s_cfg", sim=self.sim, bitwidth=DW, has_tlast=True)
        self.s_in = StreamIFSlave(name=f"{self.name}_s_in", sim=self.sim, bitwidth=DW, has_tlast=True)
        # Unframed: queue out carries no packet boundary to the bus, and the RTL kernel has no
        # TLAST pin on this port (mm_fir_xsi.render_top ties it off).
        self.m_out = StreamIFMaster(name=f"{self.name}_m_out", sim=self.sim, bitwidth=DW,
                                    has_tlast=False)
        # The response FIFO: one FirRespHdr per packet.  Unframed, as any queue out is.
        self.m_resp = StreamIFMaster(name=f"{self.name}_m_resp", sim=self.sim, bitwidth=DW,
                                     has_tlast=False)
        self.m_status = StreamIFMaster(name=f"{self.name}_m_status", sim=self.sim, bitwidth=DW,
                                       has_tlast=True)
        for ep in (self.s_cfg, self.s_in, self.m_out, self.m_resp, self.m_status):
            self.add_endpoint(ep)
        self.taps = np.zeros(0, dtype=np.int64)
        #: The last NTAP_MAX - 1 samples, oldest first: the filter's memory across packets.
        self.hist = np.zeros(NTAP_MAX - 1, dtype=np.int64)
        self.nsamp = 0
        self.ncfg = 0
        #: The id of the config in force; 0 = none yet (the RTL's reset state).
        self.cfg_id = 0

    def _publish(self):
        yield from self.m_status.write(FirStatus(nsamp=self.nsamp, cfg_id=self.cfg_id,
                                                 ncfg=self.ncfg & 0xFFFF))

    def _filter(self, x: np.ndarray) -> np.ndarray:
        """Filter one packet with the taps in force: :func:`fir_golden` over the history and the
        packet -- one vectorized convolution -- keeping the history continuous across packets."""
        h, n = len(self.hist), len(x)
        xs = np.concatenate([self.hist, x])
        y = fir_golden(xs, [(0, self.taps)])[h:]
        self.hist = xs[n:]
        self.nsamp += n
        return y

    def run_iter(self):
        hdr = yield from self.s_in.get_schema(FirCmdHdr)
        # The order the two streams cannot give, carried in the header: wait for this packet's
        # config.  A config committed for a LATER packet stays in s_cfg until one asks for it.
        while self.cfg_id != int(hdr.cfg_id):
            cfg = yield from self.s_cfg.get_schema(FirCfg)
            self.taps = np.asarray(cfg.coeffs, dtype=np.int64)[:int(cfg.ntaps)]
            self.cfg_id = int(cfg.cfg_id)
            self.ncfg += 1
        n = int(hdr.nsamp)
        T = self.clk.period
        t_loop = self.env.now + self.hdr_cycles * T          # the earliest the sample loop starts
        if n:
            x, tstart = yield from self.s_in.get_pipelined(S16, n)      # the serializer unpacks
            y = self._filter(np.asarray(x.val, dtype=np.int64))
            # Timing, as the HLS body: the loop takes its first sample once the header is handled and
            # the sample has arrived; the first result leaves proc_latency cycles later, then one per
            # proc_ii cycles.
            t_out_start = max(tstart, t_loop) + self.proc_latency * T
            proc_time = max(0.0, n * self.proc_ii * T + (t_out_start - self.env.now))
            yield self.timeout(proc_time)
            yield from self.m_out.write_pipelined(array(S64, y), t_out_start)
        yield self.timeout(self.tail_cycles * T)
        # The status first, then the response -- so a host holding a packet's response knows the
        # status already counts it, and reads the final status once instead of waiting for it.
        yield from self._publish()
        # The response: which packet, and which config it was ACTUALLY filtered with.
        yield from self.m_resp.write(FirRespHdr(nsamp=n, tx_id=int(hdr.tx_id), cfg_id=self.cfg_id))
        yield self.timeout(self.restart_cycles * T)


#: The FIR type's address layout (offsets within the slave), and its views' absolute addresses at
#: :data:`MM_BASE` -- what the RTL crossbar and the testbench are configured with.
MM_LAYOUT = MemSlaveLayout.of(MmFir, mem_dwidth=DW)
REGS, QIN, QOUT, QRESP = (MM_BASE + MM_LAYOUT[n].base for n in ("regs", "qin", "qout", "qresp"))


# ---------------------------------------------------------------------------
# The host and the system
# ---------------------------------------------------------------------------

def host_schedule(nsamp: int, plan, pkt: int, lag: int = 0, stale_tag: bool = False) -> list[tuple]:
    """What the host sends, in order: ``("cfg", taps, cfg_id)`` and ``("pkt", n0, n1, tag, want)`` --
    *tag* is the ``cfg_id`` the packet's header carries, *want* the config the plan means it to use
    (they differ only under *stale_tag*).

    *plan* is ``[(apply_at, taps), ...]``: config *i*, named ``cfg_id = i + 1``, is in
    force from sample ``apply_at``.  Samples go out in packets of at most *pkt*, cut at every
    ``apply_at`` so a packet sees one config, and each packet is tagged with the config it needs.

    Each config is committed when the samples before its ``apply_at`` have been sent -- or *lag*
    samples later (a late commit, which the kernel must wait for).  *stale_tag* is the negative
    control: every packet is tagged with config 1, so no packet ever asks for config 2.

    The reader reads one output packet per ``"pkt"`` entry, the same size, because queue out is
    unframed and the reader has to know.
    """
    cfgs = sorted(plan, key=lambda c: c[0])
    if not cfgs or cfgs[0][0] != 0:
        raise ValueError("the first config must apply at sample 0")
    starts = [at for at, _ in cfgs]

    def due(i: int) -> int:
        return cfgs[i][0] + (lag if i else 0)

    def seq_at(n: int) -> int:
        return sum(1 for at in starts if at <= n)

    out: list[tuple] = []
    nxt = n = 0
    while n < nsamp:
        while nxt < len(cfgs) and due(nxt) <= n:
            out.append(("cfg", cfgs[nxt][1], nxt + 1))
            nxt += 1
        cuts = [at for at in starts if at > n] + [due(nxt) if nxt < len(cfgs) else nsamp]
        end = min([n + pkt, nsamp] + cuts)
        out.append(("pkt", n, end, 1 if stale_tag else seq_at(n), seq_at(n)))
        n = end
    while nxt < len(cfgs):                          # a config committed after the last sample
        out.append(("cfg", cfgs[nxt][1], nxt + 1))
        nxt += 1
    return out


def _u64(words) -> np.ndarray:
    """Words as uint64 -- every piece of a scenario burst, so a concatenation cannot promote to float
    (numpy turns int64 + uint64 into float64, which loses the low bits of a 64-bit word)."""
    return np.asarray([int(w) for w in np.asarray(words).reshape(-1)], dtype=np.uint64)


#: A scenario item's kind -- the first word of each burst of a host scenario bundle.
CFG, PKT = 0, 1
#: The host's endpoints that record a trace (memory-mapped wiring), in the order they are dumped.
HOST_ENDPOINTS = ("cfg", "qin", "qout", "qresp", "status")


@dataclass
class FirHost(HwModule):
    """The host program: configure, stream samples, switch taps mid-stream, collect the results.

    Two processes, as stream code is written: a **writer** that commits each config and sends each
    sample packet behind its :class:`FirCmdHdr` -- never asking whether a config has arrived, because
    the header's ``cfg_id`` makes the kernel wait for it -- and a **reader** that takes one output
    packet per input packet, and that packet's response, which it checks.  **Nothing polls**: over the
    bus, the endpoints sleep on the queue views' interrupts (``plans/mm_irq.md``) -- queue in's for room,
    queue out's and the response FIFO's for data -- and the final status is read once, because the
    kernel publishes it before each response.  A response must echo: the response must echo the
    packet's ``tx_id`` and the config the plan meant it to use.  The host holds five endpoints and
    never an address, so the same class runs memory-mapped (through the adaptor) and direct (joined
    straight to the kernel); :class:`MmFirSystem` sets them:

    * ``cfg`` -- a ``StreamIFMaster``: one ``write`` is one committed config;
    * ``qin`` -- a ``StreamIFMaster``: one ``write`` is one queue-in packet (a header, or samples);
    * ``qout`` -- a ``StreamIFSlave``, unframed: reads name their size;
    * ``qresp`` -- a ``StreamIFSlave``, unframed: one :class:`FirRespHdr` per packet;
    * ``status`` -- a :class:`~waveflow.hw.mm_host.LatestValueIFSlave`: the latest status.

    **Two realizations** (``plans/xsi_system_top.md``).  This class is the Python one; its
    :meth:`bfm_model` names the C++ one, ``FirHostModel`` in ``mm_fir_host.h`` beside this file, which
    an XSI system top's generated harness instantiates.  Both run **one scenario**: the schedule as
    word messages, one burst per item (:meth:`scenario_bursts`) -- ``[CFG, <config words>]`` or
    ``[PKT, nsamp, tx_id, want, nhdr, <header words>, <sample words>]``.  Given :attr:`scenario` (a
    bundle :meth:`write_scenario` wrote), this host runs from that file, as the C++ one does;
    otherwise from the same bursts built in memory.  Given :attr:`trace_dir`, each memory-mapped
    endpoint's trace -- what crossed it -- is dumped there after the run, one bundle per endpoint
    (:data:`HOST_ENDPOINTS`); the C++ host dumps the same five, and the conformance gate compares
    them byte for byte.
    """

    x: list = field(default_factory=list)
    plan: list = field(default_factory=list)
    pkt: int = 16
    #: Commit each config (after the first) this many samples AFTER its packets went out.  The
    #: packets wait in queue in until it arrives, and the output is still exact.
    lag: int = 0
    #: Negative control: tag every packet with config 1, so config 2 is never taken.
    stale_tag: bool = False
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))
    #: Cycles between polls, should an endpoint poll (none does here: they wait on interrupts).  One
    #: setting for both hosts: the bus endpoints' ``poll_cycles`` and the C++ model's argument.
    poll_cycles: int = 8
    #: The scenario bundle both hosts run (``write_scenario``); empty: built from x / plan / pkt.
    scenario: DynParam[str] = ""
    #: Where each endpoint's trace is dumped after the run; empty: not dumped.
    trace_dir: DynParam[str] = ""

    def __post_init__(self) -> None:
        super().__post_init__()
        #: The bus master, when the system is memory-mapped (unbound when it is direct).
        self.m = MMIFMaster(name=f"{self.name}_m", sim=self.sim, bitwidth=DW,
                            max_outstanding=HOST_MAX_OUTSTANDING, issue_cycles=HOST_ISSUE_CYCLES)
        self.cfg: StreamIFMaster | None = None
        self.qin: StreamIFMaster | None = None
        self.qout: StreamIFSlave | None = None
        self.qresp: StreamIFSlave | None = None
        self.status = None
        #: The host's ends of the queue views' interrupt lines (memory-mapped wiring binds them; direct
        #: wiring leaves them unbound).  Attributes rather than only a dict, because a BFM model names
        #: its ports by attribute.
        self.irq_qin = IrqIFSink(name=f"{self.name}_irq_qin", sim=self.sim)
        self.irq_qout = IrqIFSink(name=f"{self.name}_irq_qout", sim=self.sim)
        self.irq_qresp = IrqIFSink(name=f"{self.name}_irq_qresp", sim=self.sim)
        self.irq: dict[str, IrqIFSink] = {"qin": self.irq_qin, "qout": self.irq_qout,
                                          "qresp": self.irq_qresp}
        for ep in (self.m, self.irq_qin, self.irq_qout, self.irq_qresp):
            self.add_endpoint(ep)
        self.done = self.env.event()
        self.y: list[int] = []
        #: Every response, as ``(tx_id, cfg_id)``.
        self.responses: list[tuple[int, int]] = []
        #: Responses that did not echo what the host expected: ``(tx_id, field, expected, got)``.
        self.mismatches: list[tuple[int, str, int, int]] = []
        self.status_reads = 0
        self.schedule = host_schedule(len(self.x), self.plan, self.pkt, self.lag, self.stale_tag)

    # -- the scenario: the schedule as word messages, one burst per item ---------------------------

    def scenario_bursts(self) -> list[np.ndarray]:
        """The schedule as the words both hosts send -- see the class docstring for the layout."""
        out, tx = [], 0
        for item in self.schedule:
            if item[0] == "cfg":
                words = make_cfg(item[1], cfg_id=item[2]).serialize(word_bw=DW)
                out.append(np.concatenate([_u64([CFG]), _u64(words)]))
            else:
                _, n0, n1, tag, want = item
                hdr = _u64(FirCmdHdr(nsamp=n1 - n0, tx_id=tx & 0xFFFF, cfg_id=tag).serialize(word_bw=DW))
                smp = _u64(array(S16, np.asarray(self.x[n0:n1], dtype=np.int64)).serialize(word_bw=DW))
                out.append(np.concatenate([_u64([PKT, n1 - n0, tx & 0xFFFF, want, len(hdr)]), hdr, smp]))
                tx += 1
        return out

    def write_scenario(self, path) -> None:
        """Write :meth:`scenario_bursts` as a burst bundle at *path* -- the file both hosts run."""
        from waveflow.utils.burst_io import write_burst_bundle
        write_burst_bundle(self.scenario_bursts(), path)

    def pre_sim(self) -> None:
        super().pre_sim()
        if self.scenario:
            from waveflow.utils.burst_io import read_burst_bundle
            bursts = read_burst_bundle(self.scenario)
        else:
            bursts = self.scenario_bursts()
        #: The decoded scenario: ("cfg", words) or ("pkt", nsamp, tx_id, want, header, samples).
        self.items: list[tuple] = []
        for b in bursts:
            b = np.asarray(b, dtype=np.uint64)
            if int(b[0]) == CFG:
                self.items.append(("cfg", b[1:]))
            else:
                nsamp, tx, want, nhdr = (int(v) for v in b[1:5])
                self.items.append(("pkt", nsamp, tx, want, b[5:5 + nhdr], b[5 + nhdr:]))

    def post_sim(self) -> None:
        super().post_sim()
        if self.trace_dir:
            from pathlib import Path
            for name in HOST_ENDPOINTS:
                write_trace(getattr(self, name), Path(self.trace_dir) / name)

    # -- the host program ---------------------------------------------------------------------------

    def _read_status(self):
        self.status_reads += 1
        return (yield from self.status.read())

    def _writer(self):
        for item in self.items:
            if item[0] == "cfg":
                yield from self.cfg.write(item[1])
            else:
                _, _nsamp, _tx, _want, hdr, samples = item
                yield from self.qin.write(hdr)
                yield from self.qin.write(samples)

    def _reader(self):
        for item in self.items:
            if item[0] == "pkt":
                _, nsamp, tx, want, _hdr, _samples = item
                y = yield from self.qout.get_array(S64, nsamp)
                self.y += [int(v) for v in y.val]
                resp = yield from self.qresp.get_schema(FirRespHdr)
                got = (int(resp.tx_id), int(resp.cfg_id))
                self.responses.append(got)
                for name, exp, val in (("tx_id", tx, got[0]), ("cfg_id", want, got[1])):
                    if exp != val:
                        self.mismatches.append((got[0], name, exp, val))
        # The kernel publishes its status before each response, so after the last response the
        # status is final: one read, no waiting for it.
        self.final_status = yield from self._read_status()
        self.done.succeed()

    def bfm_model(self):
        """The C++ realization: ``FirHostModel`` in ``mm_fir_host.h`` beside this file -- the same
        writer and reader on ``xsi_mm_host.h``'s endpoints, run from the same scenario bundle.

        It spans the bus master (an ``AxiMmMaster``, one read and one write in flight -- what
        ``HOST_MAX_OUTSTANDING`` means for ``MMIFMaster``) and the three interrupt pins it waits on."""
        from waveflow.build.composite_gen import BfmModel

        if int(self.m.max_outstanding) != 1:
            raise ValueError(f"FirHostModel's AxiMmMaster keeps one read and one write in flight; "
                             f"{self.name}.m.max_outstanding is {self.m.max_outstanding}")
        return BfmModel("FirHostModel", ports=("m", "irq_qin", "irq_qout", "irq_qresp"),
                        extra_args=(str(int(self.poll_cycles)),), header="mm_fir_host.h")

    def run_proc(self):
        self.env.process(self._writer())
        yield from self._reader()


@dataclass
class MmFirSystem:
    """Everything wired: the host, the kernel, and between them either the bus or nothing.

    ``link="mm"`` (default): host -> crossbar -> {register bank, queue in, queue out} <-> kernel,
    each view on its own crossbar slot, or all three behind one adaptor port with ``one_front``.
    ``link="direct"``: the host's four endpoints joined straight to the kernel's -- plain streams,
    and a :class:`~waveflow.hw.mm_host.LatestValueIF` for the status.  The host class is the same.
    """

    x: list
    plan: list
    pkt: int = 16
    lag: int = 0
    stale_tag: bool = False
    #: Stage 4: all three views behind one adaptor port instead of one crossbar slot each.
    one_front: bool = False
    link: str = "mm"
    xbar_latency: float = XBAR_LATENCY
    xbar_travel: float = XBAR_TRAVEL
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))

    def __post_init__(self) -> None:
        if self.link not in ("mm", "direct"):
            raise ValueError(f"link must be 'mm' or 'direct', got {self.link!r}")
        if self.one_front and self.link != "mm":
            raise ValueError("one_front chooses a memory-mapped topology; it needs link='mm'")
        sim = self.sim = Simulation()
        self.fir = MmFir(name="fir", sim=sim, clk=self.clk)
        self.host = FirHost(name="host", sim=sim, x=list(self.x), plan=list(self.plan), pkt=self.pkt,
                            lag=self.lag, stale_tag=self.stale_tag, clk=self.clk)
        if self.link == "direct":
            self._wire_direct()
        else:
            self._wire_mm()

    def _stream(self, name: str, master, slave, depth: int) -> None:
        si = StreamIF(name=name, sim=self.sim, clk=self.clk, bitwidth=DW, depth=depth)
        si.bind(ep_name="master", endpoint=master)
        si.bind(ep_name="slave", endpoint=slave)

    def _wire_direct(self) -> None:
        sim, host, fir = self.sim, self.host, self.fir
        host.cfg = StreamIFMaster(name="host_cfg", sim=sim, bitwidth=DW, has_tlast=True)
        host.qin = StreamIFMaster(name="host_qin", sim=sim, bitwidth=DW, has_tlast=True)
        host.qout = StreamIFSlave(name="host_qout", sim=sim, bitwidth=DW, has_tlast=False)
        host.qresp = StreamIFSlave(name="host_qresp", sim=sim, bitwidth=DW, has_tlast=False)
        host.status = LatestValueIFSlave(name="host_status", sim=sim)
        self._stream("k_cfg", host.cfg, fir.s_cfg, FirCfg.nwords_per_inst(DW))
        self._stream("k_in", host.qin, fir.s_in, QDEPTH)
        self._stream("k_out", fir.m_out, host.qout, QDEPTH)
        self._stream("k_resp", fir.m_resp, host.qresp, RDEPTH)
        self.status_if = LatestValueIF(name="k_stat", sim=sim, schema_type=FirStatus, bitwidth=DW,
                                       clk=self.clk)
        self.status_if.bind("master", fir.m_status)
        self.status_if.bind("slave", host.status)

    def _wire_mm(self) -> None:
        sim, clk = self.sim, self.clk
        # The kernel's memory-mapped side, built from the views its type declares: behind one front,
        # or one crossbar slot per view -- the same addresses either way (view k at k * 4 KB).
        self.device = build_mm_device(self.fir, sim=sim, clk=clk, mem_dwidth=DW,
                                      one_front=self.one_front)
        self.regs, self.qin, self.qout, self.qresp = (
            self.device.views[n] for n in ("regs", "qin", "qout", "qresp"))
        self.adaptor = self.device.adaptor
        slaves, ranges = self.device.ranges(MM_BASE)
        self.xbar = AXIMMCrossBarIF(name="xbar", sim=sim, clk=clk, nports_master=1,
                                    nports_slave=len(slaves), bitwidth=DW,
                                    latency_init=self.xbar_latency,
                                    latency_travel=self.xbar_travel)
        self.xbar.bind("master_0", self.host.m)
        for k, ep in enumerate(slaves):
            self.xbar.bind(f"slave_{k}", ep)
        assign_address_ranges(slaves, ranges)
        self.slave_map = self.device.layout.at(MM_BASE)
        mm = BoundMemSlaveAdaptor(self.slave_map, self.host.m, poll_cycles=self.host.poll_cycles)
        self.host.cfg = mm.stream_master("regs")
        # Each queue view's interrupt line, to the host: the endpoints sleep on these, never poll.
        for v in (self.qin, self.qout, self.qresp):
            line = IrqIF(name=f"{v.name}_irq", sim=sim)
            line.bind("source", v.m_irq)
            line.bind("sink", self.host.irq[v.name])
        self.host.qin = mm.stream_master("qin", irq=self.host.irq["qin"])
        self.host.qout = mm.stream_slave("qout", irq=self.host.irq["qout"])
        self.host.qresp = mm.stream_slave("qresp", irq=self.host.irq["qresp"])
        self.host.status = mm.status("regs")

    def run(self) -> np.ndarray:
        self.sim.run_sim(until=self.host.done)
        return np.asarray(self.host.y, dtype=np.int64)


def demo(seed: int = 7, nsamp: int = 200, switch_at: int = 96) -> dict:
    """Rung 2: two tap sets, switched at ``switch_at``.  Returns the run's facts for a caller."""
    rng = np.random.default_rng(seed)
    x = rng.integers(-2000, 2000, size=nsamp)
    plan = [(0, [3, -1, 4, 1, -5]), (switch_at, [2, 7, 1, -8, 2, 8, 1, -8])]
    sysm = MmFirSystem(x=list(x), plan=plan)
    y = sysm.run()
    return {"x": x, "y": y, "golden": fir_golden(x, plan), "status": sysm.host.final_status,
            "cycles": sysm.sim.env.now / sysm.clk.period}


if __name__ == "__main__":
    r = demo()
    ok = np.array_equal(r["y"], r["golden"])
    st = r["status"]
    print(f"bit-exact={ok}  nsamp={int(st.nsamp)} ncfg={int(st.ncfg)} late={int(st.late)}  "
          f"cycles={r['cycles']:.0f}")
