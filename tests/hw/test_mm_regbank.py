"""pysim tests for the memory-mapped register bank (``waveflow.hw.mm_regbank``).

``plans/mm_slave_adaptor.md`` Stage 2.  ``test_regbank_matches_the_rtl_gate`` replays the RTL gate's
scenario (``tests/build/test_mm_regbank_xsi.py``) and asserts the same observable results: snapshot
isolation, two commits never merged, the shadow read-back, the commit count, and the latest complete
status message.  The rest cover the typed interface the kernel sees.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pytest

from waveflow.hw.clock import Clock
from waveflow.hw.dataschema import DataList, IntField
from waveflow.hw.interface import StreamIF, StreamIFMaster, StreamIFSlave
from waveflow.hw.memif import AXIMMCrossBarIF, MMIFMaster, assign_address_ranges
from waveflow.hw.mm_regbank import MemSlaveRegBank
from waveflow.simulation.simobj import SimObj
from waveflow.simulation.simulation import Simulation

DW = 64
CLK = Clock(freq=100e6)
U64 = IntField.specialize(bitwidth=64, signed=False)
U16 = IntField.specialize(bitwidth=16, signed=False)


class Cfg4(DataList):
    elements = {"c0": U64, "c1": U64, "c2": U64, "c3": U64}


class Stat2(DataList):
    elements = {"s0": U64, "s1": U64}


@dataclass
class LateSink(SimObj):
    start: float = 0.0
    cfg_type: type | None = None

    def __post_init__(self) -> None:
        super().__post_init__()
        self.ep = StreamIFSlave(name=f"{self.name}_ep", sim=self.sim, bitwidth=DW, has_tlast=True)
        self.packets: list[list[int]] = []
        self.decoded: list = []

    def run_proc(self):
        if self.start > 0:
            yield self.env.timeout(self.start)
        while True:
            if self.cfg_type is not None:
                self.decoded.append((yield from self.ep.get_schema(self.cfg_type)))
            else:
                burst = yield from self.ep.get()
                self.packets.append([int(w) for w in np.asarray(burst)])


@dataclass
class Source(SimObj):
    words: list = field(default_factory=list)

    def __post_init__(self) -> None:
        super().__post_init__()
        self.ep = StreamIFMaster(name=f"{self.name}_ep", sim=self.sim, bitwidth=DW, has_tlast=True)

    def run_proc(self):
        if self.words:
            yield from self.ep.write(np.asarray(self.words, dtype=np.uint64))


@dataclass
class Host(SimObj):
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


def _system(ops, *, cfg_from_cycles=0, status_words=(), cfg_depth=None, decode=False,
            latency_init=0.0):
    sim = Simulation()
    regs = MemSlaveRegBank(name="regs", sim=sim, cfg_type=Cfg4, status_type=Stat2, mem_dwidth=DW,
                           clk=CLK)
    sink = LateSink(name="cfg_sink", sim=sim, start=cfg_from_cycles * CLK.period,
                    cfg_type=Cfg4 if decode else None)
    src = Source(name="stat_src", sim=sim, words=list(status_words))
    host = Host(name="host", sim=sim, ops=ops)
    c = StreamIF(name="k_cfg", sim=sim, clk=CLK, bitwidth=DW,
                 depth=regs.ncfg if cfg_depth is None else cfg_depth)
    c.bind(ep_name="master", endpoint=regs.m_cfg)
    c.bind(ep_name="slave", endpoint=sink.ep)
    st = StreamIF(name="k_stat", sim=sim, clk=CLK, bitwidth=DW, depth=8)
    st.bind(ep_name="master", endpoint=src.ep)
    st.bind(ep_name="slave", endpoint=regs.s_status)
    xbar = AXIMMCrossBarIF(name="xbar", sim=sim, clk=CLK, nports_master=1, nports_slave=1,
                           bitwidth=DW, latency_init=latency_init)
    xbar.bind("master_0", host.m)
    xbar.bind("slave_0", regs.s_mem)
    assign_address_ranges([regs.s_mem], [(0, 0x1000)])
    sim.run_sim()
    return host, sink, regs


REGS, COMMIT, STATUS = 0x0, 0x800, 0xC00


def test_regbank_matches_the_rtl_gate():
    ops = [("w", REGS, [0xC0, 0xC1, 0xC2, 0xC3]),
           ("w", COMMIT, [1]),
           ("w", REGS + 8, [0xAA]),          # after commit 1: must not reach packet 1
           ("w", COMMIT, [1]),               # stalls until packet 1 is taken
           ("r", REGS, 4),
           ("r", COMMIT, 1),
           ("r", STATUS, 2)]
    host, sink, regs = _system(ops, cfg_from_cycles=100, status_words=[0x51, 0x52, 0x61, 0x62])
    assert sink.packets == [[0xC0, 0xC1, 0xC2, 0xC3], [0xC0, 0xAA, 0xC2, 0xC3]]
    assert host.results[4] == [0xC0, 0xAA, 0xC2, 0xC3]
    assert host.results[5] == [2]
    assert host.results[6] == [0x61, 0x62]
    # The second commit's write completes only once packet 1 has been taken (sink opens at 100).
    assert host.t_end[3] >= 100 * CLK.period
    assert host.t_end[2] < 100 * CLK.period


def test_kernel_reads_a_typed_config_and_host_reads_typed_status():
    cfg = Cfg4(c0=7, c1=8, c2=9, c3=10)
    probe = MemSlaveRegBank(name="p", sim=Simulation(), cfg_type=Cfg4, status_type=Stat2)
    ops = [("w", REGS, [int(w) for w in probe.cfg_words(cfg)]), ("w", COMMIT, [1])]
    host, sink, regs = _system(ops, decode=True, status_words=[3, 4])
    assert len(sink.decoded) == 1
    got = sink.decoded[0]
    assert [int(got.c0), int(got.c1), int(got.c2), int(got.c3)] == [7, 8, 9, 10]
    st = regs.status()
    assert [int(st.s0), int(st.s1)] == [3, 4]


def test_cfg_channel_must_hold_exactly_one_packet():
    with pytest.raises(ValueError, match="one config packet"):
        _system([("w", COMMIT, [1])], cfg_depth=8)


def test_status_is_peekable_for_polling():
    host, _sink, regs = _system([("r", STATUS, 2)], status_words=[5, 6])
    assert [int(x) for x in regs.s_mem.peek_read(2, STATUS)] == [5, 6]


def test_window_must_fit_the_schemas():
    class Big(DataList):
        elements = {f"f{i}": U64 for i in range(300)}
    with pytest.raises(ValueError, match="do not fit"):
        MemSlaveRegBank(name="b", sim=Simulation(), cfg_type=Big, status_type=Stat2)


#: Completion cycle of each op in the RTL gate (op durations from its LAT line, accumulated).
RTL_LAT = {"w0": 9, "c1": 5, "w1": 5, "c2": 87, "rs": 9, "rc": 5, "st": 6}


def test_pysim_timing_tracks_the_rtl_gate():
    """With the crossbar at latency_init = 4 the per-op costs match the RTL gate to within 2 cycles --
    except the stalled commit (c2), which pysim releases up to one packet early: RTL accepts commit 2
    only after packet 1's LAST word is taken (the sink drains it a word per cycle), while a pysim sink
    takes the whole packet in one event.  The same burst-granularity limit as the queue's stalled
    write; the bound pins its direction and size."""
    ops = [("w", REGS, [0xC0, 0xC1, 0xC2, 0xC3]), ("w", COMMIT, [1]), ("w", REGS + 8, [0xAA]),
           ("w", COMMIT, [1]), ("r", REGS, 4), ("r", COMMIT, 1), ("r", STATUS, 2)]
    host, *_ = _system(ops, cfg_from_cycles=100, status_words=[0x51, 0x52, 0x61, 0x62],
                       latency_init=4)
    ends = [t / CLK.period for t in host.t_end]
    durs = [ends[0]] + [b - a for a, b in zip(ends, ends[1:])]
    for (name, rtl), dur in zip(RTL_LAT.items(), durs):
        # RTL op k starts the cycle after op k-1 ends, so its duration is directly comparable.
        if name == "c2":
            assert 0 <= rtl - dur <= Cfg4.nwords_per_inst(DW) + 2, f"c2: pysim {dur} vs RTL {rtl}"
            continue
        assert abs(dur - rtl) <= 2, f"{name}: pysim {dur} cycles vs RTL {rtl}"
