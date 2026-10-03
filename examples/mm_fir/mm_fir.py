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

**The stream_inband pattern, with a config sequence number** (``plans/mm_fir_cfg_seq.md``).  Every
sample packet is preceded in-band by a :class:`FirCmdHdr` -- its sample count and ``cfg_seq``, the
number of the config it needs (config *k* is the *k*-th COMMIT).  Per packet the kernel reads the
header, takes configs from ``s_cfg`` until it has config ``cfg_seq`` -- **waiting** if it has not
arrived -- then reads the samples, filters them and writes the results.

**Cross-view order is carried in the messages** (the slave page's ordering statement 2).  A config and
and the samples travel on different streams, so either could reach the kernel first.  The sequence number
makes that harmless: a packet cannot use a config older than the one it names (the kernel waits for
it), nor a newer one (a config nobody has asked for stays in its stream).  So the host commits a config
and sends the packets that need it, in either order, and never has to ask whether the config arrived.

**And the host can check it.**  After each packet the kernel writes a :class:`FirRespHdr` to a second
queue out, the response FIFO: the packet's ``tx_id`` and the ``cfg_seq`` it actually filtered with.
The host compares each response with the config it meant the packet to use.  The wait makes a wrong
config impossible from the kernel's side; the echo catches a host that asked for the wrong one.

Numbers: samples are int16, one per 64-bit word (low bits); taps are int16; outputs are the exact
integer convolution, int64 two's complement in a 64-bit word.  No rounding anywhere, so the numpy
golden :func:`fir_golden` is bit-exact by construction.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar

import numpy as np

from waveflow.hw.clock import Clock
from waveflow.hw.dataschema import DataArray, DataList, IntField
from waveflow.hw.hw_freerun import FreeRunMod
from waveflow.hw.hw_module import HwParam
from waveflow.hw.interface import StreamIF, StreamIFMaster, StreamIFSlave
from waveflow.hw.memif import AXIMMCrossBarIF, MMIFMaster, assign_address_ranges
from waveflow.hw.mm_adaptor import MemSlaveAdaptor
from waveflow.hw.mm_host import (
    BoundMemSlaveAdaptor,
    LatestValueIF,
    LatestValueIFSlave,
    MemSlaveMap,
)
from waveflow.hw.mm_queue import MemSlaveRStream, MemSlaveWStream
from waveflow.hw.mm_regbank import MemSlaveRegBank
from waveflow.simulation.simobj import SimObj
from waveflow.simulation.simulation import Simulation

DW = 64
NTAP_MAX = 16
QDEPTH = 64

S16 = IntField.specialize(bitwidth=16, signed=True)
U32 = IntField.specialize(bitwidth=32, signed=False)
#: One sample word on ``s_in`` / one result word on ``m_out``: 64 bits, the sample in the low 16.
Word = IntField.specialize(bitwidth=64, signed=False)
Taps = DataArray.specialize(S16, max_shape=(NTAP_MAX,))


U16 = IntField.specialize(bitwidth=16, signed=False)


class FirCmdHdr(DataList):
    """The in-band header in front of every sample packet on ``s_in`` -- one 64-bit word."""

    elements = {
        "nsamp": {"schema": U32, "description": "samples in this packet"},
        "tx_id": {"schema": U16, "description": "the host's packet id, echoed in the response"},
        "cfg_seq": {"schema": U16,
                    "description": "the config this packet needs: config k is the k-th COMMIT"},
    }


class FirRespHdr(DataList):
    """The kernel's response to one packet, on the response FIFO -- one 64-bit word."""

    elements = {
        "nsamp": {"schema": U32, "description": "samples filtered in this packet"},
        "tx_id": {"schema": U16, "description": "echo of the packet's tx_id"},
        "cfg_seq": {"schema": U16, "description": "the config the packet was filtered with"},
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
    }


class FirStatus(DataList):
    """What the kernel publishes after every packet (latest value wins)."""

    elements = {
        "nsamp": {"schema": U32, "description": "samples filtered so far"},
        "ncfg": {"schema": U32, "description": "configs taken so far"},
    }


REGS, QIN, QOUT, QRESP = 0x0000, 0x1000, 0x2000, 0x3000
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


def make_cfg(taps) -> FirCfg:
    taps = list(taps)
    if not 1 <= len(taps) <= NTAP_MAX:
        raise ValueError(f"1..{NTAP_MAX} taps, got {len(taps)}")
    coeffs = np.zeros(NTAP_MAX, dtype=np.int64)
    coeffs[:len(taps)] = taps
    return FirCfg(ntaps=len(taps), coeffs=coeffs)


def samples_of(words) -> np.ndarray:
    """Sample words -> int16 values as int64: the low 16 bits of each 64-bit word are the sample."""
    return (np.asarray(words, dtype=np.uint64) & 0xFFFF).astype(np.uint16).view(np.int16).astype(np.int64)


# ---------------------------------------------------------------------------
# The kernel
# ---------------------------------------------------------------------------

@dataclass
class MmFir(FreeRunMod):
    """The FIR kernel: streams only.  One firing = one packet: its header, the config it needs, its
    samples."""

    cpp_kernel_name: ClassVar[str | None] = "mm_fir"
    cpp_namespace: ClassVar[str | None] = "mm_fir_impl"

    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))
    ntap_max: HwParam[int] = NTAP_MAX
    #: Timing of the HLS body, from its csynth report: pipelined at II=1.  A sample's result leaves
    #: ``proc_latency`` cycles after the sample arrived, and one sample is taken per ``proc_ii`` cycles.
    proc_ii: int = 1
    proc_latency: int = 10

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

    def _publish(self):
        yield from self.m_status.write(FirStatus(nsamp=self.nsamp, ncfg=self.ncfg))

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
        while self.ncfg < int(hdr.cfg_seq):
            cfg = yield from self.s_cfg.get_schema(FirCfg)
            self.taps = np.asarray(cfg.coeffs, dtype=np.int64)[:int(cfg.ntaps)]
            self.ncfg += 1
        n = int(hdr.nsamp)
        if n:
            words, tstart = yield from self.s_in.get_pipelined(Word, n)
            y = self._filter(samples_of(words.val))
            # Timing, as the HLS body: the first result leaves proc_latency cycles after the first
            # sample arrived, and one result follows every proc_ii cycles.
            t_out_start = tstart + self.proc_latency * self.clk.period
            proc_time = max(0.0, n * self.proc_ii * self.clk.period + (t_out_start - self.env.now))
            yield self.timeout(proc_time)
            yield from self.m_out.write_pipelined(y.view(np.uint64), t_out_start)
        # The response: which packet, and which config it was ACTUALLY filtered with.
        yield from self.m_resp.write(FirRespHdr(nsamp=n, tx_id=int(hdr.tx_id), cfg_seq=self.ncfg))
        yield from self._publish()


# ---------------------------------------------------------------------------
# The host and the system
# ---------------------------------------------------------------------------

def host_schedule(nsamp: int, plan, pkt: int, lag: int = 0, stale_tag: bool = False) -> list[tuple]:
    """What the host sends, in order: ``("cfg", taps)`` and ``("pkt", n0, n1, tag, want)`` -- *tag* is
    the ``cfg_seq`` the packet's header carries, *want* the config the plan means it to use (they
    differ only under *stale_tag*).

    *plan* is ``[(apply_at, taps), ...]``: config *i* (the *i+1*-th COMMIT, ``cfg_seq = i + 1``) is in
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
            out.append(("cfg", cfgs[nxt][1]))
            nxt += 1
        cuts = [at for at in starts if at > n] + [due(nxt) if nxt < len(cfgs) else nsamp]
        end = min([n + pkt, nsamp] + cuts)
        out.append(("pkt", n, end, 1 if stale_tag else seq_at(n), seq_at(n)))
        n = end
    while nxt < len(cfgs):                          # a config committed after the last sample
        out.append(("cfg", cfgs[nxt][1]))
        nxt += 1
    return out


@dataclass
class FirHost(SimObj):
    """The host program: configure, stream samples, switch taps mid-stream, collect the results.

    Two processes, as stream code is written: a **writer** that commits each config and sends each
    sample packet behind its :class:`FirCmdHdr` -- never asking whether a config has arrived, because
    the header's ``cfg_seq`` makes the kernel wait for it -- and a **reader** that takes one output
    packet per input packet, and that packet's response, which it checks: the response must echo the
    packet's ``tx_id`` and the config the plan meant it to use.  The host holds five endpoints and
    never an address, so the same class runs memory-mapped (through the adaptor) and direct (joined
    straight to the kernel); :class:`MmFirSystem` sets them:

    * ``cfg`` -- a ``StreamIFMaster``: one ``write`` is one committed config;
    * ``qin`` -- a ``StreamIFMaster``: one ``write`` is one queue-in packet (a header, or samples);
    * ``qout`` -- a ``StreamIFSlave``, unframed: reads name their size;
    * ``qresp`` -- a ``StreamIFSlave``, unframed: one :class:`FirRespHdr` per packet;
    * ``status`` -- a :class:`~waveflow.hw.mm_host.LatestValueIFSlave`: the latest status.
    """

    x: list = field(default_factory=list)
    plan: list = field(default_factory=list)
    pkt: int = 16
    poll_cycles: int = 8
    #: Commit each config (after the first) this many samples AFTER its packets went out.  The
    #: packets wait in queue in until it arrives, and the output is still exact.
    lag: int = 0
    #: Negative control: tag every packet with config 1, so config 2 is never taken.
    stale_tag: bool = False
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        #: The bus master, when the system is memory-mapped (unbound when it is direct).
        self.m = MMIFMaster(name=f"{self.name}_m", sim=self.sim, bitwidth=DW)
        self.cfg: StreamIFMaster | None = None
        self.qin: StreamIFMaster | None = None
        self.qout: StreamIFSlave | None = None
        self.qresp: StreamIFSlave | None = None
        self.status = None
        self.done = self.env.event()
        self.y: list[int] = []
        #: Every response, as ``(tx_id, cfg_seq)``.
        self.responses: list[tuple[int, int]] = []
        #: Responses that did not echo what the host expected: ``(tx_id, field, expected, got)``.
        self.mismatches: list[tuple[int, str, int, int]] = []
        self.status_reads = 0
        self.schedule = host_schedule(len(self.x), self.plan, self.pkt, self.lag, self.stale_tag)

    def _read_status(self):
        self.status_reads += 1
        return (yield from self.status.read())

    def _writer(self):
        for item in self.schedule:
            if item[0] == "cfg":
                yield from self.cfg.write(make_cfg(item[1]))
            else:
                _, n0, n1, tag, _want = item
                yield from self.qin.write(FirCmdHdr(nsamp=n1 - n0, tx_id=self._tx_id(n0),
                                                    cfg_seq=tag))
                chunk = np.asarray([int(v) & 0xFFFF for v in self.x[n0:n1]], dtype=np.uint64)
                yield from self.qin.write(chunk)

    def _tx_id(self, n0: int) -> int:
        """A packet's id: its index among the packets, mod 2**16."""
        return [it[1] for it in self.schedule if it[0] == "pkt"].index(n0) & 0xFFFF

    def _reader(self):
        for item in self.schedule:
            if item[0] == "pkt":
                _, n0, n1, _tag, want = item
                words = yield from self.qout.get(nwords_max=n1 - n0)
                self.y += [int(np.int64(np.uint64(w))) for w in np.asarray(words)]
                resp = yield from self.qresp.get_schema(FirRespHdr)
                got = (int(resp.tx_id), int(resp.cfg_seq))
                self.responses.append(got)
                for name, exp, val in (("tx_id", self._tx_id(n0), got[0]), ("cfg_seq", want, got[1])):
                    if exp != val:
                        self.mismatches.append((got[0], name, exp, val))
        # The kernel publishes its status after writing a packet's outputs; wait for that last one.
        while True:
            st = yield from self._read_status()
            if int(st.nsamp) >= len(self.x):
                break
            yield self.timeout(self.poll_cycles * self.clk.period)
        self.final_status = st
        self.done.succeed()

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
    xbar_latency: float = 4.0
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
        sim, clk, fir = self.sim, self.clk, self.fir
        self.regs = MemSlaveRegBank(name="regs", sim=sim, cfg_type=FirCfg, status_type=FirStatus,
                                    mem_dwidth=DW, clk=clk)
        self.qin = MemSlaveWStream(name="qin", sim=sim, mem_dwidth=DW, depth=QDEPTH, clk=clk)
        self.qout = MemSlaveRStream(name="qout", sim=sim, mem_dwidth=DW, depth=QDEPTH, clk=clk)
        self.qresp = MemSlaveRStream(name="qresp", sim=sim, mem_dwidth=DW, depth=RDEPTH, clk=clk)
        self._stream("k_cfg", self.regs.m_cfg, fir.s_cfg, self.regs.ncfg)
        self._stream("k_stat", fir.m_status, self.regs.s_status, 8)
        self._stream("k_in", self.qin.m_out, fir.s_in, QDEPTH)
        self._stream("k_out", fir.m_out, self.qout.s_in, QDEPTH)
        self._stream("k_resp", fir.m_resp, self.qresp.s_in, RDEPTH)
        views = [self.regs, self.qin, self.qout, self.qresp]
        if self.one_front:
            # Stage 4: the views behind ONE bus port, at the same addresses (view k at k*4 KB).
            self.adaptor = MemSlaveAdaptor(name="fir_mm", sim=sim, mem_dwidth=DW, views=views)
            slaves, ranges = [self.adaptor.s_mem], [(REGS, self.adaptor.span())]
        else:
            slaves = [v.s_mem for v in views]
            ranges = [(REGS, 0x1000), (QIN, 0x1000), (QOUT, 0x1000), (QRESP, 0x1000)]
        self.xbar = AXIMMCrossBarIF(name="xbar", sim=sim, clk=clk, nports_master=1,
                                    nports_slave=len(slaves), bitwidth=DW,
                                    latency_init=self.xbar_latency)
        self.xbar.bind("master_0", self.host.m)
        for k, ep in enumerate(slaves):
            self.xbar.bind(f"slave_{k}", ep)
        assign_address_ranges(slaves, ranges)
        self.slave_map = (self.adaptor.slave_map() if self.one_front
                          else MemSlaveMap.from_views(views))
        mm = BoundMemSlaveAdaptor(self.slave_map, self.host.m, poll_cycles=self.host.poll_cycles)
        self.host.cfg = mm.stream_master("regs")
        self.host.qin = mm.stream_master("qin")
        self.host.qout = mm.stream_slave("qout")
        self.host.qresp = mm.stream_slave("qresp")
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
