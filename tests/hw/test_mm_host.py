"""pysim tests for the bus master's endpoints onto a slave adaptor (plans/mm_adaptor_host_endpoints.md
Stage 1): the address map, each view's endpoint, and the no-stall property that is the reason the
endpoints poll."""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pytest

from waveflow.hw.clock import Clock
from waveflow.hw.dataschema import DataList, IntField
from waveflow.hw.interface import StreamIF, StreamIFMaster, StreamIFSlave
from waveflow.hw.memif import AXIMMCrossBarIF, MMIFMaster, assign_address_ranges
from waveflow.hw.mm_adaptor import MemSlaveAdaptor
from waveflow.hw.mm_bram import MemSlaveBramWindow
from waveflow.hw.mm_host import (
    BoundMemSlaveAdaptor,
    LatestValueIF,
    LatestValueIFSlave,
    MemSlaveMap,
    MmStreamIFSlave,
)
from waveflow.hw.mm_queue import MemSlaveRStream, MemSlaveWStream
from waveflow.hw.mm_regbank import MemSlaveRegBank
from waveflow.simulation.simobj import SimObj
from waveflow.simulation.simulation import Simulation

DW = 64
DEPTH = 16
BASE = 0x4000_0000
U32 = IntField.specialize(bitwidth=32, signed=False)
U64 = IntField.specialize(bitwidth=64, signed=False)


class Cfg(DataList):
    elements = {"gain": U32, "offset": U32}


class Status(DataList):
    elements = {"nsamp": U32, "npkt": U32}


@dataclass
class Scale(SimObj):
    """Waits for a config, then for each packet writes ``gain * x + offset`` and a status."""

    def __post_init__(self):
        super().__post_init__()
        self.s_cfg = StreamIFSlave(name=f"{self.name}_s_cfg", sim=self.sim, bitwidth=DW)
        self.m_status = StreamIFMaster(name=f"{self.name}_m_status", sim=self.sim, bitwidth=DW)
        self.s_in = StreamIFSlave(name=f"{self.name}_s_in", sim=self.sim, bitwidth=DW)
        self.m_out = StreamIFMaster(name=f"{self.name}_m_out", sim=self.sim, bitwidth=DW,
                                    has_tlast=False)
        self.ncfg = 0

    def run_proc(self):
        cfg = yield from self.s_cfg.get_schema(Cfg)
        self.ncfg += 1
        nsamp = npkt = 0
        while True:
            pkt = np.asarray((yield from self.s_in.get()), dtype=np.uint64)
            yield from self.m_out.write(pkt * np.uint64(int(cfg.gain)) + np.uint64(int(cfg.offset)))
            nsamp, npkt = nsamp + len(pkt), npkt + 1
            yield from self.m_status.write(Status(nsamp=nsamp, npkt=npkt))


@dataclass
class Host(SimObj):
    """Runs the generators in :attr:`procs` as separate processes on one bus master."""

    procs: list = field(default_factory=list)

    def __post_init__(self):
        super().__post_init__()
        self.m = MMIFMaster(name=f"{self.name}_m", sim=self.sim, bitwidth=DW)
        self.log: dict = {}

    def pre_sim(self):
        super().pre_sim()
        for p in self.procs:
            self.env.process(p(self))


@dataclass
class Rig:
    """An adaptor with all four views, a Scale kernel and a host on one crossbar port."""

    procs: list
    qdepth: int = DEPTH
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))

    def __post_init__(self):
        sim = self.sim = Simulation()
        clk = self.clk
        self.regs = MemSlaveRegBank(name="regs", sim=sim, cfg_type=Cfg, status_type=Status, clk=clk)
        self.qin = MemSlaveWStream(name="qin", sim=sim, depth=self.qdepth, clk=clk)
        self.qout = MemSlaveRStream(name="qout", sim=sim, depth=self.qdepth, clk=clk)
        self.bram = MemSlaveBramWindow(name="bram", sim=sim, nelem=256, clk=clk)
        self.adaptor = MemSlaveAdaptor(name="mm", sim=sim,
                                       views=[self.regs, self.qin, self.qout, self.bram])
        self.kern = Scale(name="kern", sim=sim)
        self.host = Host(name="host", sim=sim, procs=self.procs)
        for nm, m, s, d in (("c", self.regs.m_cfg, self.kern.s_cfg, self.regs.ncfg),
                            ("s", self.kern.m_status, self.regs.s_status, 4),
                            ("i", self.qin.m_out, self.kern.s_in, self.qdepth),
                            ("o", self.kern.m_out, self.qout.s_in, self.qdepth)):
            si = StreamIF(name=nm, sim=sim, clk=clk, bitwidth=DW, depth=d)
            si.bind(ep_name="master", endpoint=m)
            si.bind(ep_name="slave", endpoint=s)
        xbar = AXIMMCrossBarIF(name="xbar", sim=sim, clk=clk, nports_master=1, nports_slave=1,
                               bitwidth=DW, latency_init=4)
        xbar.bind("master_0", self.host.m)
        xbar.bind("slave_0", self.adaptor.s_mem)
        assign_address_ranges([self.adaptor.s_mem], [(BASE, self.adaptor.span())])
        self.mm = BoundMemSlaveAdaptor(self.adaptor.slave_map(), self.host.m)

    def run(self, until: float = 2e-4):
        self.sim.run_sim(until=until)
        return self.host.log


# ---------------------------------------------------------------------------
# The map
# ---------------------------------------------------------------------------

def test_map_from_adaptor_names_kinds_and_addresses():
    r = Rig(procs=[])
    m = r.adaptor.slave_map()
    assert sorted(m.views) == ["bram", "qin", "qout", "regs"]
    assert [(m[n].kind, m[n].base) for n in ("regs", "qin", "qout", "bram")] == [
        ("regbank", BASE), ("queue_in", BASE + 0x1000), ("queue_out", BASE + 0x2000),
        ("bram", BASE + 0x3000)]
    assert (m["regs"].commit_addr, m["regs"].status_addr) == (BASE + 0x800, BASE + 0xC00)
    assert m["qout"].status_addr == BASE + 0x2800
    assert (m["regs"].ncfg, m["regs"].nstat, m["qin"].depth, m["bram"].nelem) == (1, 1, DEPTH, 256)
    assert m["qout"].max_burst == 256 and m["qin"].max_burst == 256


def test_map_from_separate_views_reads_each_views_own_base():
    sim = Simulation()
    qin = MemSlaveWStream(name="qin", sim=sim)
    qout = MemSlaveRStream(name="qout", sim=sim)
    with pytest.raises(RuntimeError, match="assign_address_ranges"):
        MemSlaveMap.from_views([qin, qout])
    assign_address_ranges([qin.s_mem, qout.s_mem], [(0x1000, 0x1000), (0x8000, 0x1000)])
    m = MemSlaveMap.from_views([qin, qout])
    assert (m["qin"].base, m["qout"].base) == (0x1000, 0x8000)
    with pytest.raises(KeyError, match="no view named 'regs'"):
        m["regs"]


def test_adaptor_refuses_duplicate_view_names():
    sim = Simulation()
    with pytest.raises(ValueError, match="named 'q'"):
        MemSlaveAdaptor(name="ad", sim=sim, views=[MemSlaveWStream(name="q", sim=sim),
                                                   MemSlaveRStream(name="q", sim=sim)])


def test_proxy_names_the_right_call_for_the_wrong_kind():
    r = Rig(procs=[])
    with pytest.raises(TypeError, match="use stream_slave"):
        r.mm.stream_master("qout")
    with pytest.raises(TypeError, match="use region"):
        r.mm.stream_slave("bram")
    assert r.mm.stream_master("qin") is r.mm.stream_master("qin")


# ---------------------------------------------------------------------------
# Each view through its endpoint
# ---------------------------------------------------------------------------

def _configure(h, gain=3, offset=0):
    yield from h.mm.stream_master("regs").write(Cfg(gain=gain, offset=offset))


def test_queue_in_and_out_round_trip_including_a_packet_longer_than_the_queue():
    def writer(h):
        yield from _configure(h)
        yield from h.mm.stream_master("qin").write(np.arange(1, 6, dtype=np.uint64))
        yield from h.mm.stream_master("qin").write(np.arange(10, 30, dtype=np.uint64))  # 20 > 16

    def reader(h):
        y = yield from h.mm.stream_slave("qout").get_array(U64, 25)
        h.log["y"] = [int(v) for v in y.val]

    r = Rig(procs=[writer, reader])
    r.host.mm = r.mm
    log = r.run()
    assert log["y"] == [3 * v for v in list(range(1, 6)) + list(range(10, 30))]
    assert r.qin.packets == [5, 20]


def test_get_returns_exactly_n_across_kernel_bursts():
    def writer(h):
        yield from _configure(h, gain=1)
        for k in range(3):
            yield from h.mm.stream_master("qin").write(np.full(4, k, dtype=np.uint64))

    def reader(h):
        qout = h.mm.stream_slave("qout")
        h.log["a"] = [int(w) for w in (yield from qout.get(nwords_max=6))]   # spans 2 bursts
        h.log["b"] = [int(w) for w in (yield from qout.get(nwords_max=6))]

    r = Rig(procs=[writer, reader])
    r.host.mm = r.mm
    log = r.run()
    assert log == {"a": [0, 0, 0, 0, 1, 1], "b": [1, 1, 2, 2, 2, 2]}


def test_queue_out_is_unframed_so_a_read_must_name_its_size():
    def reader(h):
        with pytest.raises(ValueError, match="nwords_max"):
            yield from h.mm.stream_slave("qout").get()
        h.log["nb"] = yield from h.mm.stream_slave("qout").get_array_nb(U64, 1)

    r = Rig(procs=[reader])
    r.host.mm = r.mm
    assert r.run() == {"nb": None}
    assert isinstance(r.mm.stream_slave("qout"), MmStreamIFSlave)
    assert isinstance(r.mm.stream_slave("qout"), StreamIFSlave)


def test_config_write_is_one_message_and_status_is_latest_value():
    def host(h):
        yield from _configure(h, gain=2, offset=7)
        st = h.mm.status("regs")
        h.log["before"] = int((yield from st.read()).npkt)
        for k in range(3):
            yield from h.mm.stream_master("qin").write(np.asarray([k], dtype=np.uint64))
        y = yield from h.mm.stream_slave("qout").get_array(U64, 3)
        h.log["y"] = [int(v) for v in y.val]
        s1 = yield from st.read()
        s2 = yield from st.read()                # reading does not consume
        h.log["status"] = [(int(s.nsamp), int(s.npkt)) for s in (s1, s2)]

    r = Rig(procs=[host])
    r.host.mm = r.mm
    log = r.run()
    assert log["before"] == 0
    assert log["y"] == [7, 9, 11]
    assert log["status"] == [(3, 3), (3, 3)]
    assert r.kern.ncfg == 1 and r.regs.ncommit == 1


def test_a_config_of_the_wrong_size_is_refused():
    def host(h):
        with pytest.raises(ValueError, match="1 words, got 2"):
            yield from h.mm.stream_master("regs").write(np.zeros(2, dtype=np.uint64))
        h.log["ok"] = True

    r = Rig(procs=[host])
    r.host.mm = r.mm
    assert r.run() == {"ok": True}


def test_bram_window_is_a_region():
    def host(h):
        buf = h.mm.region("bram", U64)
        yield from buf.write_slice(3, np.asarray([5, 6, 7]))
        h.log["back"] = [int(v) for v in (yield from buf.read_slice(2, 6))]

    r = Rig(procs=[host])
    r.host.mm = r.mm
    assert r.run() == {"back": [0, 5, 6, 7]}
    assert [int(r.bram.port_b_read(i)) for i in range(3, 6)] == [5, 6, 7]


# ---------------------------------------------------------------------------
# The no-stall property (D3) and its negative control
# ---------------------------------------------------------------------------

NPKT, PKT, QD = 6, 8, 8


def _reader_late(h):
    """Starts popping only after the queues have had time to fill, then takes everything."""
    yield h.env.timeout(5e-6)
    y = yield from h.mm.stream_slave("qout").get_array(U64, NPKT * PKT)
    h.log["y"] = len(y.val)


def test_polling_writer_and_reader_share_one_front_without_deadlock():
    """The kernel fills queue out (nobody reads yet) and stops taking input; queue in fills too.
    A writer that polls never issues a write a full queue would stall, so the reader's pops still
    get through the one front, and everything drains."""
    def writer(h):
        yield from _configure(h, gain=1)
        for k in range(NPKT):
            yield from h.mm.stream_master("qin").write(np.full(PKT, k, dtype=np.uint64))
        h.log["wrote"] = NPKT

    r = Rig(procs=[writer, _reader_late], qdepth=QD)
    r.host.mm = r.mm
    log = r.run(until=1e-3)
    assert log == {"wrote": NPKT, "y": NPKT * PKT}


def test_negative_control_a_stalling_writer_deadlocks_the_same_host():
    """Same scenario, but the writer writes ``[len | data]`` straight to the window without asking
    for room.  Its write to a full queue in holds the front, so the reader's pop can never be
    served: the deadlock the polling exists to avoid."""
    def writer(h):
        yield from _configure(h, gain=1)
        qin = h.mm.slave_map["qin"].base
        for k in range(NPKT):
            pkt = np.concatenate([[PKT], np.full(PKT, k)]).astype(np.uint64)
            yield from h.m.write(pkt, qin)
        h.log["wrote"] = NPKT

    r = Rig(procs=[writer, _reader_late], qdepth=QD)
    r.host.mm = r.mm
    log = r.run(until=1e-3)
    assert "wrote" not in log and "y" not in log, log


# ---------------------------------------------------------------------------
# LatestValueIF: the direct-connection twin of the status half
# ---------------------------------------------------------------------------

def test_latest_value_if_keeps_only_the_latest_complete_message():
    sim = Simulation()
    clk = Clock(freq=100e6)
    lv = LatestValueIF(name="lv", sim=sim, schema_type=Status, bitwidth=DW, clk=clk)
    src = StreamIFMaster(name="src", sim=sim, bitwidth=DW)
    rd = LatestValueIFSlave(name="rd", sim=sim)
    lv.bind("master", src)
    lv.bind("slave", rd)
    seen = []

    def proc():
        seen.append(int((yield from rd.read()).npkt))
        for k in range(1, 4):
            yield from src.write(Status(nsamp=10 * k, npkt=k))   # never blocks on a reader
        seen.append(int((yield from rd.read()).npkt))
        seen.append(int((yield from rd.read()).nsamp))

    sim.env.process(proc())
    sim.run_sim()
    assert seen == [0, 3, 30] and lv.nmsg == 3


# ---------------------------------------------------------------------------
# The map as a C++ header (the XSI host's address map)
# ---------------------------------------------------------------------------

def test_cpp_header_carries_every_view_and_no_derived_offset():
    """One MmView per view with the base and sizes; the offsets inside a window (COMMIT, status) are
    NOT in the header -- MmView computes them, as ViewEntry does, so they are written once per
    language and cannot be restated wrong in a generated file."""
    r = Rig(procs=[])
    h = r.adaptor.slave_map().to_cpp_header("rig_map", source="test")
    assert '#include "xsi_mm_host.h"' in h and "namespace rig_map {" in h
    assert ('static const wfbfm::MmView regs = {"regs", wfbfm::MmKind::RegBank, 0x40000000ull, '
            '4096u, 8u, 0u, 1u, 1u, 0u};') in h
    assert ('static const wfbfm::MmView qout = {"qout", wfbfm::MmKind::QueueOut, 0x40002000ull, '
            '4096u, 8u, 16u, 0u, 0u, 0u};') in h
    assert "wfbfm::MmKind::Bram, 0x40003000ull" in h and "wfbfm::MmKind::QueueIn, 0x40001000ull" in h
    assert "0x40000800" not in h and "0x40000c00" not in h


def test_cpp_header_refuses_a_view_name_that_is_not_an_identifier():
    sim = Simulation()
    q = MemSlaveWStream(name="queue-in", sim=sim)
    assign_address_ranges([q.s_mem], [(0, 0x1000)])
    with pytest.raises(ValueError, match=r"not a C\+\+ identifier"):
        MemSlaveMap.from_views([q]).to_cpp_header("m")
