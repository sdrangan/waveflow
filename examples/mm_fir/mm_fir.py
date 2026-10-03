"""mm_fir.py — a free-running FIR whose taps are registers and whose samples are a queue.

The witness example of ``plans/mm_slave_adaptor.md``.  The kernel is an ordinary stream-only
``FreeRunMod``; every memory-mapped view it has is an adaptor module in front of it, reached by a
host through one crossbar:

====================  =========  ===========================================================
view                  base       kernel side
====================  =========  ===========================================================
register bank         0x0000     ``s_cfg`` <- one :class:`FirCfg` per commit;
                                 ``m_status`` -> :class:`FirStatus` (latest-value)
queue in              0x1000     ``s_in``  <- sample packets ``[len | x x len]``
queue out             0x2000     ``m_out`` -> one output word per sample
====================  =========  ===========================================================

(The same 1x3 shape as the Stage 2 RTL gate, ``tests/build/test_mm_regbank_xsi.py``.)

**Cross-view order is carried in the messages** (the plan's decision D5).  A configuration and the
samples travel on different streams, so the kernel could see them in either order.  A config therefore
says *where* it applies -- ``apply_at``, a sample index -- and the kernel switches taps exactly there.
The host makes that happen in time by waiting until the status shows the config was **received**
(``ncfg``) before sending the sample at ``apply_at``.  A config that arrives after its sample has
already been filtered is applied at once and counted in ``late``: detected, never silently misapplied.

The kernel polls both streams, config first -- the pysim model of an HLS ``read_nb`` loop -- so it
picks up a commit even while no samples are arriving, which is exactly what the host's wait needs.

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
Taps = DataArray.specialize(S16, max_shape=(NTAP_MAX,))


class FirCfg(DataList):
    """One configuration: ``ntaps`` taps from ``coeffs``, in force from sample ``apply_at`` on."""

    elements = {
        "ntaps": {"schema": U32, "description": "active taps (<= NTAP_MAX)"},
        "apply_at": {"schema": U32, "description": "index of the first sample filtered with these taps"},
        "coeffs": {"schema": Taps, "description": "tap k multiplies x[n-k]"},
    }


class FirStatus(DataList):
    """What the kernel publishes after every event (latest value wins)."""

    elements = {
        "nsamp": {"schema": U32, "description": "samples filtered so far"},
        "ncfg": {"schema": U32, "description": "configs received so far"},
        "late": {"schema": U32, "description": "configs that arrived after their apply_at sample"},
    }


REGS, QIN, QOUT = 0x0000, 0x1000, 0x2000


# ---------------------------------------------------------------------------
# Golden
# ---------------------------------------------------------------------------

def fir_golden(x, cfgs) -> np.ndarray:
    """Exact FIR over the whole stream: sample *n* uses the latest config with ``apply_at <= n``.

    The filter history is continuous across a switch (only the taps change), and samples before the
    first config see all-zero taps.  *cfgs* is ``[(apply_at, taps), ...]``.
    """
    x = np.asarray(x, dtype=np.int64)
    y = np.zeros(len(x), dtype=np.int64)
    order = sorted(cfgs, key=lambda c: c[0])
    for n in range(len(x)):
        taps = np.zeros(0, dtype=np.int64)
        for at, t in order:
            if at <= n:
                taps = np.asarray(t, dtype=np.int64)
        for k, c in enumerate(taps):
            if n - k >= 0:
                y[n] += int(c) * int(x[n - k])
    return y


def make_cfg(taps, apply_at: int) -> FirCfg:
    taps = list(taps)
    if not 1 <= len(taps) <= NTAP_MAX:
        raise ValueError(f"1..{NTAP_MAX} taps, got {len(taps)}")
    coeffs = np.zeros(NTAP_MAX, dtype=np.int64)
    coeffs[:len(taps)] = taps
    return FirCfg(ntaps=len(taps), apply_at=int(apply_at), coeffs=coeffs)


def _s16(w: int) -> int:
    w &= 0xFFFF
    return w - 0x10000 if w & 0x8000 else w


# ---------------------------------------------------------------------------
# The kernel
# ---------------------------------------------------------------------------

@dataclass
class MmFir(FreeRunMod):
    """The FIR kernel: streams only.  One firing = one config, one sample packet, or one idle cycle."""

    cpp_kernel_name: ClassVar[str | None] = "mm_fir"
    cpp_namespace: ClassVar[str | None] = "mm_fir_impl"

    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))
    ntap_max: HwParam[int] = NTAP_MAX

    def kernel_task(self):
        """The hand-written HLS body, ``include/mm_fir_task.h`` -- the twin of :meth:`run_iter`."""
        from waveflow.hw.mem_stream import KernelTask
        return KernelTask("mm_fir_task", "mm_fir_task.h", ("s_cfg", "s_in", "m_out", "m_status"),
                          template_args=(DW,))

    def __post_init__(self) -> None:
        super().__post_init__()
        self.s_cfg = StreamIFSlave(name=f"{self.name}_s_cfg", sim=self.sim, bitwidth=DW, has_tlast=True)
        self.s_in = StreamIFSlave(name=f"{self.name}_s_in", sim=self.sim, bitwidth=DW, has_tlast=True)
        # Unframed: queue out carries no packet boundary to the bus, and the RTL kernel has no
        # TLAST pin on this port (mm_fir_xsi.render_top ties it off).
        self.m_out = StreamIFMaster(name=f"{self.name}_m_out", sim=self.sim, bitwidth=DW,
                                    has_tlast=False)
        self.m_status = StreamIFMaster(name=f"{self.name}_m_status", sim=self.sim, bitwidth=DW,
                                       has_tlast=True)
        for ep in (self.s_cfg, self.s_in, self.m_out, self.m_status):
            self.add_endpoint(ep)
        self.taps = np.zeros(0, dtype=np.int64)
        #: ONE pending slot -- ``(apply_at, taps)`` received but not yet in force -- as in the RTL.
        #: A config arriving while one is pending first puts the pending one in force.
        self.pending: tuple[int, np.ndarray] | None = None
        self.hist = np.zeros(NTAP_MAX, dtype=np.int64)      # hist[k] = x[n-1-k]
        self.nsamp = 0
        self.ncfg = 0
        self.late = 0

    def _publish(self):
        yield from self.m_status.write(FirStatus(nsamp=self.nsamp, ncfg=self.ncfg, late=self.late))

    def run_iter(self):
        cfg = yield from self.s_cfg.get_schema_nb(FirCfg)
        if cfg is not None:
            self.ncfg += 1
            taps = np.asarray(cfg.coeffs, dtype=np.int64)[:int(cfg.ntaps)]
            at = int(cfg.apply_at)
            if at < self.nsamp:
                self.late += 1
                at = self.nsamp                     # too late for its sample: in force from now
            if self.pending is not None:            # superseded: in force now, as the RTL does
                self.taps = self.pending[1]
            self.pending = (at, taps)
            yield from self._publish()
            return
        if self.s_in.data_buffer.items:
            pkt = yield from self.s_in.get()
            out = np.zeros(len(pkt), dtype=np.uint64)
            for i, w in enumerate(np.asarray(pkt).tolist()):
                if self.pending is not None and self.pending[0] <= self.nsamp:
                    self.taps, self.pending = self.pending[1], None
                xn = _s16(int(w))
                window = np.concatenate([[xn], self.hist[:NTAP_MAX - 1]])
                acc = int(np.dot(self.taps, window[:len(self.taps)])) if len(self.taps) else 0
                out[i] = np.uint64(acc & 0xFFFF_FFFF_FFFF_FFFF)
                self.hist = window[:NTAP_MAX]
                self.nsamp += 1
            yield from self.m_out.write(out)
            yield from self._publish()
            return
        yield self.timeout(self.clk.period)         # idle: poll again next cycle


# ---------------------------------------------------------------------------
# The host and the system
# ---------------------------------------------------------------------------

def host_schedule(nsamp: int, plan, pkt: int, lag: int = 0) -> list[tuple]:
    """What the host sends, in order: ``("cfg", i, apply_at, taps)`` and ``("pkt", n0, n1)``.

    Samples go out in packets of at most *pkt*; a packet never straddles a commit point, because the
    config for it is committed first.  Config *i* (after the first) is committed *lag* samples after
    its ``apply_at`` -- 0 is the correct host, >0 is the negative control.  The writer sends this list;
    the reader reads one output packet per ``"pkt"`` entry, the same size, because queue out is
    unframed and the reader has to know.
    """
    cfgs = sorted(plan, key=lambda c: c[0])
    if not cfgs or cfgs[0][0] != 0:
        raise ValueError("the first config must apply at sample 0")

    def due(i: int) -> int:
        return cfgs[i][0] + (lag if i else 0)

    out: list[tuple] = []
    nxt = n = 0
    while n < nsamp:
        while nxt < len(cfgs) and due(nxt) <= n:
            out.append(("cfg", nxt, cfgs[nxt][0], cfgs[nxt][1]))
            nxt += 1
        end = min(n + pkt, nsamp, due(nxt) if nxt < len(cfgs) else nsamp)
        out.append(("pkt", n, end))
        n = end
    return out


@dataclass
class FirHost(SimObj):
    """The host program: configure, stream samples, switch taps mid-stream, collect the results.

    Two processes, as stream code is written: a **writer** that commits each config -- then waits
    until the status shows it *received*, so it cannot miss its sample -- and sends the sample
    packets; and a **reader** that takes one output packet per input packet.  The host holds four
    endpoints and never an address, so the same class runs memory-mapped (through the adaptor) and
    direct (joined straight to the kernel); :class:`MmFirSystem` sets them:

    * ``cfg`` -- a ``StreamIFMaster``: one ``write`` is one committed config;
    * ``qin`` -- a ``StreamIFMaster``: one ``write`` is one sample packet;
    * ``qout`` -- a ``StreamIFSlave``, unframed: reads name their size;
    * ``status`` -- a :class:`~waveflow.hw.mm_host.LatestValueIFSlave`: the latest status.
    """

    x: list = field(default_factory=list)
    plan: list = field(default_factory=list)
    pkt: int = 16
    poll_cycles: int = 8
    #: Negative-control knob: commit each config (after the first) this many samples AFTER its
    #: ``apply_at``.  0 is the correct host.  >0 makes the config late, which the kernel must report.
    lag: int = 0
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        #: The bus master, when the system is memory-mapped (unbound when it is direct).
        self.m = MMIFMaster(name=f"{self.name}_m", sim=self.sim, bitwidth=DW)
        self.cfg: StreamIFMaster | None = None
        self.qin: StreamIFMaster | None = None
        self.qout: StreamIFSlave | None = None
        self.status = None
        self.done = self.env.event()
        self.y: list[int] = []
        self.status_reads = 0
        self.schedule = host_schedule(len(self.x), self.plan, self.pkt, self.lag)

    def _read_status(self):
        self.status_reads += 1
        return (yield from self.status.read())

    def _writer(self):
        for item in self.schedule:
            if item[0] == "cfg":
                _, i, apply_at, taps = item
                yield from self.cfg.write(make_cfg(taps, apply_at))
                while int((yield from self._read_status()).ncfg) < i + 1:
                    yield self.timeout(self.poll_cycles * self.clk.period)
            else:
                _, n0, n1 = item
                chunk = np.asarray([int(v) & 0xFFFF for v in self.x[n0:n1]], dtype=np.uint64)
                yield from self.qin.write(chunk)

    def _reader(self):
        for item in self.schedule:
            if item[0] == "pkt":
                words = yield from self.qout.get(nwords_max=item[2] - item[1])
                self.y += [int(np.int64(np.uint64(w))) for w in np.asarray(words)]
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
                            lag=self.lag, clk=self.clk)
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
        host.status = LatestValueIFSlave(name="host_status", sim=sim)
        self._stream("k_cfg", host.cfg, fir.s_cfg, FirCfg.nwords_per_inst(DW))
        self._stream("k_in", host.qin, fir.s_in, QDEPTH)
        self._stream("k_out", fir.m_out, host.qout, QDEPTH)
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
        self._stream("k_cfg", self.regs.m_cfg, fir.s_cfg, self.regs.ncfg)
        self._stream("k_stat", fir.m_status, self.regs.s_status, 8)
        self._stream("k_in", self.qin.m_out, fir.s_in, QDEPTH)
        self._stream("k_out", fir.m_out, self.qout.s_in, QDEPTH)
        if self.one_front:
            # Stage 4: the three views behind ONE bus port, at the same addresses (view k at k*4 KB).
            self.adaptor = MemSlaveAdaptor(name="fir_mm", sim=sim, mem_dwidth=DW,
                                           views=[self.regs, self.qin, self.qout])
            slaves, ranges = [self.adaptor.s_mem], [(REGS, self.adaptor.span())]
        else:
            slaves = [self.regs.s_mem, self.qin.s_mem, self.qout.s_mem]
            ranges = [(REGS, 0x1000), (QIN, 0x1000), (QOUT, 0x1000)]
        self.xbar = AXIMMCrossBarIF(name="xbar", sim=sim, clk=clk, nports_master=1,
                                    nports_slave=len(slaves), bitwidth=DW,
                                    latency_init=self.xbar_latency)
        self.xbar.bind("master_0", self.host.m)
        for k, ep in enumerate(slaves):
            self.xbar.bind(f"slave_{k}", ep)
        assign_address_ranges(slaves, ranges)
        self.slave_map = (self.adaptor.slave_map() if self.one_front
                          else MemSlaveMap.from_views([self.regs, self.qin, self.qout]))
        mm = BoundMemSlaveAdaptor(self.slave_map, self.host.m, poll_cycles=self.host.poll_cycles)
        self.host.cfg = mm.stream_master("regs")
        self.host.qin = mm.stream_master("qin")
        self.host.qout = mm.stream_slave("qout")
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
