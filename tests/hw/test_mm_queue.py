"""pysim tests for the memory-mapped queue windows (``waveflow.hw.mm_queue``).

``plans/mm_slave_adaptor.md`` Stage 1.  The first test replays the RTL gate's scenario
(``tests/build/test_mm_queue_xsi.py``) in pysim and asserts the same observable results: the words and
packet boundaries the kernel receives, the vacancy and occupancy reads, the words the host pops, and
the empty pop flagged as an error.  The fault cases of the RTL gate (partial WSTRB, wrong AxSIZE)
have no pysim counterpart -- the pysim bus carries neither.

The second test is the Producer -> MemWStream -> crossbar -> MemSlaveWStream -> Consumer chain: a
kernel that already writes memory through ``MemWStream`` reaches another kernel's queue just by
pointing its base address at the queue window.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pytest

from waveflow.hw.clock import Clock
from waveflow.hw.interface import StreamIF, StreamIFMaster, StreamIFSlave
from waveflow.hw.mem_stream import MemWStream, MWCmd
from waveflow.hw.memif import AXIMMCrossBarIF, MMIFMaster, assign_address_ranges
from waveflow.hw.mm_queue import MemSlaveRStream, MemSlaveWStream
from waveflow.simulation.simobj import SimObj
from waveflow.simulation.simulation import Simulation

DW = 64
DEPTH = 64
CLK = Clock(freq=100e6)
QIN, QOUT = 0x0000, 0x1000
QOUT_STATUS = QOUT + 0x800


def ramp(base: int, n: int) -> list[int]:
    return [base + i for i in range(n)]


@dataclass
class LateSink(SimObj):
    """The kernel side of a queue-in: takes nothing until *start* seconds, then every packet."""

    start: float = 0.0

    def __post_init__(self) -> None:
        super().__post_init__()
        self.ep = StreamIFSlave(name=f"{self.name}_ep", sim=self.sim, bitwidth=DW, has_tlast=True)
        self.packets: list[list[int]] = []
        self.arrival: list[float] = []

    def run_proc(self):
        if self.start > 0:
            yield self.env.timeout(self.start)
        while True:
            burst = yield from self.ep.get()
            self.packets.append([int(w) for w in np.asarray(burst)])
            self.arrival.append(self.env.now)


@dataclass
class Source(SimObj):
    """The kernel side of a queue-out: writes one burst at t = 0."""

    words: list[int] = field(default_factory=list)

    def __post_init__(self) -> None:
        super().__post_init__()
        self.ep = StreamIFMaster(name=f"{self.name}_ep", sim=self.sim, bitwidth=DW, has_tlast=True)

    def run_proc(self):
        yield from self.ep.write(np.asarray(self.words, dtype=np.uint64))


@dataclass
class Host(SimObj):
    """A bus master running a fixed list of ``("w", addr, words)`` / ``("r", addr, n)`` ops."""

    ops: list = field(default_factory=list)

    def __post_init__(self) -> None:
        super().__post_init__()
        self.m = MMIFMaster(name=f"{self.name}_m", sim=self.sim, bitwidth=DW)
        self.results: list = []
        self.t_end: list[float] = []

    def run_proc(self):
        for kind, addr, arg in self.ops:
            if kind == "w":
                yield from self.m.write(np.asarray(arg, dtype=np.uint64), addr)
                self.results.append(None)
            else:
                words = yield from self.m.read(arg, addr)
                self.results.append([int(w) for w in np.asarray(words)])
            self.t_end.append(self.env.now)


def _queue_system(ops, sink_from_cycles: int, g: list[int], latency_init: float = 0.0):
    sim = Simulation()
    qin = MemSlaveWStream(name="qin", sim=sim, mem_dwidth=DW, depth=DEPTH, clk=CLK)
    qout = MemSlaveRStream(name="qout", sim=sim, mem_dwidth=DW, depth=DEPTH, clk=CLK)
    sink = LateSink(name="sink", sim=sim, start=sink_from_cycles * CLK.period)
    src = Source(name="src", sim=sim, words=g)
    host = Host(name="host", sim=sim, ops=ops)
    s1 = StreamIF(name="k_in", sim=sim, clk=CLK, bitwidth=DW, depth=DEPTH)
    s1.bind(ep_name="master", endpoint=qin.m_out)
    s1.bind(ep_name="slave", endpoint=sink.ep)
    s2 = StreamIF(name="k_out", sim=sim, clk=CLK, bitwidth=DW, depth=DEPTH)
    s2.bind(ep_name="master", endpoint=src.ep)
    s2.bind(ep_name="slave", endpoint=qout.s_in)
    xbar = AXIMMCrossBarIF(name="xbar", sim=sim, clk=CLK, nports_master=1, nports_slave=2,
                           bitwidth=DW, latency_init=latency_init)
    xbar.bind("master_0", host.m)
    xbar.bind("slave_0", qin.s_mem)
    xbar.bind("slave_1", qout.s_mem)
    assign_address_ranges([qin.s_mem, qout.s_mem], [(QIN, 0x1000), (QOUT, 0x1000)])
    sim.run_sim()
    return host, sink, qin, qout


def _rtl_scenario():
    d, e, f = ramp(0x1000, 16), ramp(0x2000, 40), ramp(0x3000, 100)
    g = ramp(0x9000, 48)
    ops = [
        ("r", QIN, 1),                       # v0 vacancy, empty
        ("w", QIN, [16] + d),                # one packet, one burst
        ("w", QIN, [40] + e[:10]),           # a packet split ...
        ("w", QIN, e[10:]),                  # ... over two bursts
        ("r", QIN, 1),                       # v1 vacancy, 56 queued
        ("w", QIN, [100] + f),               # fills the FIFO: stalls until the sink opens
        ("r", QOUT_STATUS, 1),               # occupancy
        ("r", QOUT, 32),
        ("r", QOUT, 16),
        ("r", QOUT, 1),                      # empty pop
        ("r", QOUT_STATUS, 1),
    ]
    return d, e, f, g, ops


def test_queue_windows_match_the_rtl_gate():
    d, e, f, g, ops = _rtl_scenario()
    host, sink, qin, qout = _queue_system(ops, sink_from_cycles=600, g=g)
    r = host.results
    assert sink.packets == [d, e, f], "packets (TLAST = burst end) must match the RTL's 16 / 40 / 100"
    assert r[0] == [DEPTH]
    assert r[4] == [DEPTH - 56]
    assert r[6] == [48]
    assert r[7] == g[:32] and r[8] == g[32:]
    assert r[9] == [0] and [k for _t, k, _a in qout.errors] == ["pop_empty"]
    assert r[10] == [0]
    # Back-pressure: the 100-word packet cannot be handed over before the sink opens.
    assert host.t_end[5] >= 600 * CLK.period
    assert qin.packets == [16, 40, 100]


def test_depth_must_match_the_stream_channel():
    sim = Simulation()
    qin = MemSlaveWStream(name="qin", sim=sim, mem_dwidth=DW, depth=64, clk=CLK)
    sink = LateSink(name="sink", sim=sim)
    s = StreamIF(name="k_in", sim=sim, clk=CLK, bitwidth=DW, depth=32)
    s.bind(ep_name="master", endpoint=qin.m_out)
    s.bind(ep_name="slave", endpoint=sink.ep)
    with pytest.raises(ValueError, match="IS the FIFO"):
        qin.pre_sim()


def test_geometry_checks():
    with pytest.raises(ValueError, match="power of two"):
        MemSlaveWStream(name="q", sim=Simulation(), depth=100)
    with pytest.raises(ValueError, match="4 KB"):
        MemSlaveRStream(name="q", sim=Simulation(), window=2048)


def test_data_half_cannot_be_peeked():
    q = MemSlaveRStream(name="q", sim=Simulation(), mem_dwidth=DW)
    with pytest.raises(ValueError, match="cannot be peeked"):
        q.s_mem.peek_read(1, 0)


@dataclass
class Producer(SimObj):
    """A kernel that sends packets to another kernel's queue through MemWStream: per packet, one
    MWCmd at word offset 0 and the words ``[len | data]`` on the data stream."""

    packets: list = field(default_factory=list)

    def __post_init__(self) -> None:
        super().__post_init__()
        self.cmd = StreamIFMaster(name=f"{self.name}_cmd", sim=self.sim, bitwidth=DW, has_tlast=False)
        self.data = StreamIFMaster(name=f"{self.name}_data", sim=self.sim, bitwidth=DW, has_tlast=False)

    def run_proc(self):
        for pkt in self.packets:
            n = len(pkt) + 1
            yield from self.cmd.write(MWCmd(addr=0, len=n))
            yield from self.data.write(np.asarray([len(pkt)] + pkt, dtype=np.uint64))


def test_producer_reaches_consumer_queue_through_memwstream():
    sim = Simulation()
    base = 0x4000_0000
    pkts = [ramp(0x100, 8), ramp(0x200, 32), ramp(0x300, 3)]
    prod = Producer(name="prod", sim=sim, packets=pkts)
    wr = MemWStream(name="wr", sim=sim, mem_dwidth=DW, clk=CLK)
    wr.bind_base(base)
    qin = MemSlaveWStream(name="qin", sim=sim, mem_dwidth=DW, depth=DEPTH, clk=CLK)
    cons = LateSink(name="cons", sim=sim)
    for name, m, s, depth in (("cmd", prod.cmd, wr.s_cmd, 4), ("data", prod.data, wr.s_in, DEPTH),
                              ("k_in", qin.m_out, cons.ep, DEPTH)):
        si = StreamIF(name=name, sim=sim, clk=CLK, bitwidth=DW, depth=depth)
        si.bind(ep_name="master", endpoint=m)
        si.bind(ep_name="slave", endpoint=s)
    xbar = AXIMMCrossBarIF(name="xbar", sim=sim, clk=CLK, nports_master=1, nports_slave=1,
                           bitwidth=DW)
    xbar.bind("master_0", wr.m_mem)
    xbar.bind("slave_0", qin.s_mem)
    assign_address_ranges([qin.s_mem], [(base, 0x1000)])
    sim.run_sim()
    assert cons.packets == pkts


#: Completion cycle of each op in the RTL gate (test_mm_queue_xsi.py's END line), names in op order.
RTL_END = {"v0": 7, "w1": 29, "w2": 44, "w3": 78, "v1": 83, "w4": 695, "o0": 701, "r1": 737,
           "r2": 757, "r3": 762, "o1": 767}


def test_pysim_timing_tracks_the_rtl_gate():
    """With the crossbar's ``latency_init`` set to the 4 cycles Stage 0 measured, pysim's per-op
    durations agree with XSI to within 2 cycles -- EXCEPT the back-pressured write, and that one is a
    known limit rather than a defect: a pysim stream hands the kernel its whole queued backlog in one
    event, where RTL drains it a word per cycle, so pysim releases the stalled writer ~95 cycles
    early.  The bound below pins the direction (early, never late) and the size, so a change to the
    stream model that moves it shows up here."""
    _d, _e, _f, g, ops = _rtl_scenario()
    host, *_ = _queue_system(ops, sink_from_cycles=600, g=g, latency_init=4)
    names = list(RTL_END)
    ends = [t / CLK.period for t in host.t_end]
    rtl = [RTL_END[n] for n in names]
    for i, n in enumerate(names):
        dur_py = ends[i] - (ends[i - 1] if i else 0.0)
        dur_rtl = rtl[i] - (rtl[i - 1] if i else 0)
        if n == "w4":
            assert 0 < dur_rtl - dur_py <= 100, f"w4: pysim {dur_py} vs RTL {dur_rtl}"
            continue
        assert abs(dur_py - dur_rtl) <= 2, f"{n}: pysim {dur_py} cycles vs RTL {dur_rtl}"
