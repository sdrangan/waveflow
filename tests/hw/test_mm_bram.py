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
from pathlib import Path

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


def test_the_window_names_its_leaf_and_each_leaf_is_its_own_identity():
    """``bram_leaf`` (plans/bram_fullrate_port.md): a build input, so a fresh copy (a harness) keeps it
    and the two leaves -- different hardware, different timing -- key differently."""
    from waveflow.calib.phases import module_identity
    from waveflow.hw.hw_module import build_params

    def win(leaf=None):
        kw = {} if leaf is None else {"bram_leaf": leaf}
        return MemSlaveBramWindow(name="w", sim=Simulation(), mem_dwidth=DW, nelem=8, clk=CLK, **kw)

    assert win().bram_leaf == "mm_bram_port"
    assert build_params(win("mm_bram_port_pipe"))["bram_leaf"] == "mm_bram_port_pipe"
    keys = {leaf: module_identity(win(leaf)).key for leaf in ("mm_bram_port", "mm_bram_port_pipe")}
    assert keys["mm_bram_port"] == module_identity(win()).key
    assert keys["mm_bram_port"] != keys["mm_bram_port_pipe"]
    with pytest.raises(ValueError, match="bram_leaf"):
        win("mm_bram_port_p1")


# --- the bus side's timing: the record of the memory whose slot is the window's (Stage 4) ----------

PLATFORM = Path(__file__).resolve().parents[2] / "waveflow" / "calib" / "platforms" / \
    "xczu48dr_250mhz_vitis2025_1"


@pytest.mark.parametrize("leaf", ["mm_bram_port", "mm_bram_port_pipe"])
@pytest.mark.parametrize("dw", [128, 512])
def test_the_window_loads_the_record_of_its_memory(leaf, dw):
    """One source: the window looks up ``window_memory(leaf, dw)``'s record on the platform, through
    the path the memory loads it by, and declares what it says on its port."""
    from waveflow.calib.mem_timing import load_mem_timing
    from waveflow.hw.mm_bram import window_memory

    timing, _turn = load_mem_timing(PLATFORM, window_memory(leaf, dw))
    w = MemSlaveBramWindow(name="ring", sim=Simulation(), mem_dwidth=dw, nelem=256,
                           window=256 * dw // 8, bram_leaf=leaf, calib_dir=PLATFORM)
    w.pre_sim()
    assert w._mem_timing == timing and timing["serialize"]
    assert w.s_mem.serialize_transactions and w.s_mem.write_channel is w.s_mem.read_channel
    # Measured (xsim 2025.1): reads cost 1 + 1 a word (mm_bram_port) or 2 + 0 (the pipelined leaf)
    # beyond the bus's own cycle a word, writes 1 + 0.
    want = {"mm_bram_port": (1, 1), "mm_bram_port_pipe": (2, 0)}[leaf]
    assert (round(timing["read"]["init"]), round(timing["read"]["per_word"])) == want
    assert (round(timing["write"]["init"]), round(timing["write"]["per_word"])) == (1, 0)
    # Its own clock prices the record's cycles: 8 words at 100 MHz.
    assert w._access_delay(8, "read") == pytest.approx((want[0] + 8 * want[1]) / w.clk.freq)


def test_without_a_record_the_window_costs_nothing():
    w = MemSlaveBramWindow(name="w", sim=Simulation(), mem_dwidth=128, nelem=8, window=4096,
                           calib_dir=None)
    w.pre_sim()
    assert w._mem_timing is None and not w.s_mem.serialize_transactions
    assert w._access_delay(64, "read") == 0.0


def test_a_wide_window_holds_whole_words_on_both_ports():
    w = MemSlaveBramWindow(name="w", sim=Simulation(), mem_dwidth=128, nelem=8, window=4096)
    v = (0x1234 << 64) | 0xABCD
    w.port_b_write(3, v)
    assert w.port_b_read(3) == v
    assert w.s_mem.peek_read(1, 3 * 16).tolist() == [[0xABCD, 0x1234]]


def test_in_pysim_the_window_reads_as_its_memory_does():
    """Two readers contending over the crossbar for a 1,024-word block each: with the window in the
    memory's place, loading the memory's record, every block's read time is the memory's."""
    from waveflow.calib.fixtures.bram_window_contention import contention_system, pysim_read_times

    mem = pysim_read_times(contention_system(2, store_dir=PLATFORM))
    win = pysim_read_times(contention_system(2, store_dir=PLATFORM, window=True))
    assert win == mem and len(mem) == 2 and min(mem) > 1024


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
