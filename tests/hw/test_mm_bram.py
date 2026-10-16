"""pysim tests for the BRAM window (``waveflow.hw.mm_bram``) and the ordering guarantee's scope.

``test_ordering_pair`` replays the RTL gate ``tests/build/test_mm_bram_order_xsi.py``: host 0 writes a
256-word burst into the window, host 1 rings a doorbell two cycles later, and a reader reads the
memory highest address first once the doorbell arrives.  Behind ONE adaptor nothing is stale; on two
separate bus ports the doorbell overtakes the data.  (RTL finds 63 stale words in the second case and
pysim all 256: a pysim burst lands at once at its end, an RTL burst word by word -- the direction is
the claim, not the count.)
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pytest

from waveflow.hw.clock import Clock
from waveflow.hw.interface import StreamIF, StreamIFSlave
from waveflow.hw.memif import AXIMMCrossBarIF, MMIFMaster, assign_address_ranges
from waveflow.hw.mm_adaptor import MemSlaveAdaptor
from waveflow.hw.mm_bram import MemSlaveBramWindow
from waveflow.hw.mm_queue import MemSlaveWStream
from waveflow.simulation.simobj import SimObj
from waveflow.simulation.simulation import Simulation

DW, NW = 64, 256
CLK = Clock(freq=100e6)


@dataclass
class Writer(SimObj):
    addr: int = 0
    words: list = field(default_factory=list)
    start_cycles: int = 0

    def __post_init__(self) -> None:
        super().__post_init__()
        self.m = MMIFMaster(name=f"{self.name}_m", sim=self.sim, bitwidth=DW)
        self.t_end = None

    def run_proc(self):
        yield self.env.timeout(self.start_cycles * CLK.period)
        yield from self.m.write(np.asarray(self.words, dtype=np.uint64), self.addr)
        self.t_end = self.env.now


@dataclass
class Reader(SimObj):
    """Waits for a doorbell N, then reads port B at N-1 ... 0, two cycles per word."""

    win: object = None

    def __post_init__(self) -> None:
        super().__post_init__()
        self.bell = StreamIFSlave(name=f"{self.name}_bell", sim=self.sim, bitwidth=DW, has_tlast=True)
        self.got: list[int] = []
        self.t_first = None

    def run_proc(self):
        burst = yield from self.bell.get()
        n = int(np.asarray(burst)[0])
        for j in range(n):
            yield self.env.timeout(2 * CLK.period)
            if self.t_first is None:
                self.t_first = self.env.now
            self.got.append(self.win.port_b_read(n - 1 - j))


def _order_run(one_front: bool):
    sim = Simulation()
    bell = MemSlaveWStream(name="bell", sim=sim, mem_dwidth=DW, depth=16, clk=CLK)
    win = MemSlaveBramWindow(name="win", sim=sim, mem_dwidth=DW, nelem=512, clk=CLK)
    rd = Reader(name="rd", sim=sim, win=win)
    s = StreamIF(name="k_bell", sim=sim, clk=CLK, bitwidth=DW, depth=16)
    s.bind(ep_name="master", endpoint=bell.m_out)
    s.bind(ep_name="slave", endpoint=rd.bell)
    data = [0xD000 + i for i in range(NW)]
    h0 = Writer(name="h0", sim=sim, addr=0x1000, words=data, start_cycles=1)
    h1 = Writer(name="h1", sim=sim, addr=0x0000, words=[1, NW], start_cycles=3)
    if one_front:
        ad = MemSlaveAdaptor(name="ad", sim=sim, mem_dwidth=DW, views=[bell, win])
        slaves, ranges = [ad.s_mem], [(0, ad.span())]
    else:
        slaves, ranges = [bell.s_mem, win.s_mem], [(0, 0x1000), (0x1000, 0x1000)]
    xbar = AXIMMCrossBarIF(name="xbar", sim=sim, clk=CLK, nports_master=2, nports_slave=len(slaves),
                           bitwidth=DW, latency_init=4)
    xbar.bind("master_0", h0.m)
    xbar.bind("master_1", h1.m)
    for k, ep in enumerate(slaves):
        xbar.bind(f"slave_{k}", ep)
    assign_address_ranges(slaves, ranges)
    sim.run_sim()
    stale = sum(1 for j, v in enumerate(rd.got) if v != data[NW - 1 - j])
    return {"got": len(rd.got), "stale": stale, "data_end": h0.t_end / CLK.period,
            "bell_end": h1.t_end / CLK.period}


def test_ordering_pair():
    one = _order_run(one_front=True)
    assert one["got"] == NW and one["stale"] == 0, one
    assert one["bell_end"] > one["data_end"], "behind one front the doorbell waits for the data"
    two = _order_run(one_front=False)
    assert two["got"] == NW and two["stale"] > 0, two
    assert two["bell_end"] < two["data_end"], "on separate ports the doorbell overtakes the data"


def test_window_bounds_and_readback():
    sim = Simulation()
    win = MemSlaveBramWindow(name="w", sim=sim, mem_dwidth=DW, nelem=8, clk=CLK)
    win.port_b_write(3, 0x55)
    assert list(win.s_mem.peek_read(2, 3 * 8)) == [0x55, 0]
    assert list(win.s_mem.peek_read(2, 7 * 8)) == [0, 0]       # word 8 is outside: reads 0
    with pytest.raises(ValueError, match="power of two"):
        MemSlaveBramWindow(name="b", sim=sim, nelem=100)
    with pytest.raises(ValueError, match="exceed"):
        MemSlaveBramWindow(name="c", sim=sim, nelem=1024)


def test_serialize_transactions_is_what_orders_them():
    """The same one-adaptor system with the flag cleared lets the doorbell overtake again -- the flag,
    not the half-duplex channel alone, is what holds the slave for the whole transfer."""
    from waveflow.hw import mm_adaptor
    orig = mm_adaptor.MemSlaveAdaptor.__post_init__

    def without(self):
        orig(self)
        self.s_mem.serialize_transactions = False

    mm_adaptor.MemSlaveAdaptor.__post_init__ = without
    try:
        r = _order_run(one_front=True)
    finally:
        mm_adaptor.MemSlaveAdaptor.__post_init__ = orig
    assert r["stale"] > 0 and r["bell_end"] < r["data_end"], r
