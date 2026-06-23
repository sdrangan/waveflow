"""Tests for the early-anchored pipelined memory transfers (the memory/Region mirror
of StreamIF.write_pipelined / get_pipelined).

Covers: MMIFMaster.write_array_pipelined(t_out_start=...), read_array_anchored,
Region.write_slice_pipelined / read_slice_pipelined, and — critically — that the
t_out_start=None path is byte-for-byte the old blocking behaviour (backward compat).
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import numpy.testing as npt

from waveflow.hw.clock import Clock
from waveflow.hw.dataschema import IntField
from waveflow.hw.memif import AXIMMCrossBarIF, MMIFMaster, assign_address_ranges
from waveflow.hw.memory import MemComponent
from waveflow.simulation.simobj import ProcessGen, SimObj
from waveflow.simulation.simulation import Simulation

I32 = IntField.specialize(bitwidth=32, signed=True)


@dataclass(kw_only=True)
class _Driver(SimObj):
    """Runs a user callback (a generator method) as the single process."""
    body: object = None
    out: dict = field(default_factory=dict)

    def run_proc(self) -> ProcessGen[None]:
        yield from self.body(self)


def _harness(latency_init=0.0):
    """One master + crossbar (1x1) + MemComponent, freq=1 Hz so 1 cycle = 1 s."""
    sim = Simulation()
    clk = Clock(freq=1.0)
    mem = MemComponent(sim=sim, word_size=32, inline=False, clk=clk,
                       latency_init=0.0, latency_per_word=0.0)
    mem.alloc(1024)
    master = MMIFMaster(name="m", sim=sim, bitwidth=32)
    xbar = AXIMMCrossBarIF(sim=sim, clk=clk, nports_master=1, nports_slave=1,
                           bitwidth=32, latency_init=latency_init, latency_read_return=0.0)
    xbar.bind("master_0", master)
    xbar.bind("slave_0", mem.s_mm)
    assign_address_ranges([mem.s_mm], [(0, 1024 * 4)])
    return sim, mem, master


def test_write_anchor_overlaps_vs_blocking():
    """An early (past) anchor completes the write at tstart + nwords*period — sooner
    than a blocking write that starts at `now`."""
    sim, mem, master = _harness()
    data = np.arange(10, dtype=np.int32)
    res = {}

    def body(self):
        # advance to t=3, then write 10 words anchored at t=0 (in the past)
        yield self.timeout(3.0)
        t0, t1, nw = yield from master.write_array_pipelined(
            data, I32, addr=0, t_out_start=0.0)
        res["anchored_end"] = self.now      # expect 0 + 10 = 10
        # blocking write of 10 words starting now (t=10)
        t0b, t1b, nwb = yield from master.write_array_pipelined(data, I32, addr=64)
        res["blocking_end"] = self.now      # expect 10 + 10 = 20

    sim.add_obj(_Driver(name="d", sim=sim, body=body))
    sim.run_sim()
    assert res["anchored_end"] == 10.0      # tstart(0) + 10 words
    assert res["blocking_end"] == 20.0      # 10 (now) + 10 words  -> anchored finished sooner


def test_t_out_start_none_is_blocking():
    """Backward compatibility: t_out_start=None == the original blocking write."""
    sim, mem, master = _harness()
    data = np.arange(8, dtype=np.int32)
    res = {}

    def body(self):
        yield self.timeout(5.0)
        yield from master.write_array_pipelined(data, I32, addr=0)        # default None
        res["end"] = self.now
        # data landed correctly
        got = mem.read_array(0, I32, count=8)
        res["data"] = np.asarray(got)

    sim.add_obj(_Driver(name="d", sim=sim, body=body))
    sim.run_sim()
    assert res["end"] == 13.0               # 5 (now) + 8 words, no anchor shortening
    npt.assert_array_equal(res["data"], data)


def test_read_anchored_returns_backcalc_tstart():
    """read_array_anchored blocks for the whole burst and returns tstart back-calculated
    (= completion - (nwords-1)*period), like get_pipelined."""
    sim, mem, master = _harness()
    data = np.arange(10, dtype=np.int32)
    res = {}

    def body(self):
        yield from master.write_array_pipelined(data, I32, addr=0)
        t_pre = self.now
        got, tstart = yield from master.read_array_anchored(I32, 10, addr=0)
        res.update(end=self.now, tstart=tstart, t_pre=t_pre, data=np.asarray(got))

    sim.add_obj(_Driver(name="d", sim=sim, body=body))
    sim.run_sim()
    # read of 10 words over freq=1: tstart = end - 9*period
    assert res["tstart"] == res["end"] - 9.0
    npt.assert_array_equal(res["data"], data)


def test_region_pipelined_slices():
    """Region.write_slice_pipelined / read_slice_pipelined delegate the anchor + back-calc."""
    sim, mem, master = _harness()
    region = master.region(0, I32, word_bw=32)
    data = np.arange(6, dtype=np.int32)
    res = {}

    def body(self):
        yield self.timeout(2.0)
        yield from region.write_slice_pipelined(0, data, t_out_start=0.0)
        res["wend"] = self.now              # 0 + 6 = 6
        got, tstart = yield from region.read_slice_pipelined(0, 6)
        res.update(rend=self.now, tstart=tstart, data=np.asarray(got))

    sim.add_obj(_Driver(name="d", sim=sim, body=body))
    sim.run_sim()
    assert res["wend"] == 6.0
    assert res["tstart"] == res["rend"] - 5.0
    npt.assert_array_equal(res["data"], data)
