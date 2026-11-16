"""A slave that serves one transaction at a time, met by an HLS master's bursts (plans/dse_cg.md 1f).

* ``MMIFMaster.axi_burst``: an HLS ``m_axi`` master's transfer is several bursts at RTL, each its own
  transaction at the slave -- another master's transaction goes between two of them;
* ``MMIFSlave.turnaround``: the slave stays busy that long after each transaction -- back-to-back
  transactions are spaced by it, a lone one's latency is not;
* an early-anchored write still holds the slave for its words when its anchor is past;
* the turnaround is fitted from back-to-back bursts and filed beside the memory's timing, which loads it.

The clock runs at 1 Hz, so a cycle is a second.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from waveflow.hw.clock import Clock
from waveflow.hw.memif import AXIMMCrossBarIF, MMIFMaster, assign_address_ranges
from waveflow.hw.memory import MemoryMod
from waveflow.simulation.simobj import ProcessGen, SimObj
from waveflow.simulation.simulation import Simulation


@dataclass(kw_only=True)
class _Driver(SimObj):
    """Runs a callback (a generator method) as its one process."""

    body: object = None
    out: dict = field(default_factory=dict)

    def run_proc(self) -> ProcessGen[None]:
        yield from self.body(self)


def _harness(turnaround=None):
    """An HLS master (16-beat bursts) and a plain one on a crossbar to one memory that serves one
    transaction at a time and costs nothing but the beats on the bus."""
    sim = Simulation()
    clk = Clock(freq=1.0)
    mem = MemoryMod(sim=sim, word_size=32, inline=False, clk=clk, latency_init=0.0,
                    latency_per_word=0.0)
    mem.alloc(1024)
    mem.s_mm.serialize_transactions = True
    mem.s_mm.write_channel = mem.s_mm.read_channel
    mem.s_mm.turnaround = turnaround
    hls = MMIFMaster(name="hls", sim=sim, bitwidth=32, axi_burst=16)
    plain = MMIFMaster(name="plain", sim=sim, bitwidth=32)
    xbar = AXIMMCrossBarIF(sim=sim, clk=clk, nports_master=2, nports_slave=1, bitwidth=32)
    xbar.bind("master_0", hls)
    xbar.bind("master_1", plain)
    xbar.bind("slave_0", mem.s_mm)
    assign_address_ranges([mem.s_mm], [(0, 1024 * 4)])
    return sim, mem, hls, plain


def _run(sim, *bodies):
    for k, b in enumerate(bodies):
        _Driver(name=f"d{k}", sim=sim, body=b)       # a SimObj made with sim= is in it already
    sim.run_sim()


def test_alone_an_hls_read_takes_what_one_transaction_takes():
    sim, _mem, hls, plain = _harness()
    t = {}

    def a(self):
        yield from hls.read(64, 0)
        t["hls"] = self.now
        s = self.now
        yield from plain.read(64, 0)
        t["plain"] = self.now - s

    _run(sim, a)
    assert t == {"hls": 64.0, "plain": 64.0}


def test_another_master_waits_for_one_burst_not_the_transfer():
    """The plain master's one word goes between two of the HLS master's 16-beat bursts."""
    for burst, want in ((16, 17.0), (None, 65.0)):
        sim, _mem, hls, plain = _harness()
        hls.axi_burst = burst
        t = {}

        def a(self, hls=hls):
            yield from hls.read(64, 0)

        def b(self, plain=plain, t=t):
            yield self.timeout(1.0)
            yield from plain.read(1, 0)
            t["plain"] = self.now

        _run(sim, a, b)
        assert t["plain"] == want, (burst, t)


def test_turnaround_spaces_back_to_back_transactions_only():
    """Each of the HLS master's bursts waits out the one before's turnaround (4 bursts, 3 turnarounds);
    a lone transaction's latency is unchanged, and the master goes on while the slave turns around."""
    sim, _mem, hls, plain = _harness(turnaround={"read": 2.0, "write": 3.0})
    t = {}

    def a(self):
        yield from hls.read(64, 0)
        t["hls"] = self.now
        yield self.timeout(10.0)
        s = self.now
        yield from plain.read(64, 0)
        t["plain"] = self.now - s
        s = self.now
        yield from hls.write(np.arange(32, dtype=np.uint32), 0)    # behind the read's turnaround
        t["write"] = self.now - s

    _run(sim, a)
    assert t == {"hls": 64.0 + 3 * 2.0, "plain": 64.0, "write": 2.0 + 32.0 + 3.0}


def test_an_anchored_write_holds_the_slave_for_its_words():
    """An anchor in the past may not make a write free on a slave that serves one at a time."""
    sim, _mem, hls, plain = _harness()
    t = {}

    def a(self):
        yield self.timeout(100.0)
        s = self.now
        yield from hls.write(np.arange(32, dtype=np.uint32), 0, tstart=0.0)
        t["past"] = self.now - s
        s = self.now
        yield from plain.write(np.arange(8, dtype=np.uint32), 0, tstart=self.now + 5.0)
        t["future"] = self.now - s

    _run(sim, a)
    assert t == {"past": 32.0, "future": 8.0 + 5.0}


def test_the_turnaround_is_fitted_filed_and_loaded(tmp_path):
    from waveflow.calib.mem_timing import (
        fit_turnaround,
        mem_timing_record,
        mem_turnaround_record,
    )
    from waveflow.calib.record_store import ModuleStore

    timing = {"read": {"init": 1.0, "per_word": 1.0}, "write": {"init": 1.0, "per_word": 0.0},
              "serialize": True, "max_abs_residual": {}, "n_fit": {}}
    rows = [("read", 16, 34), ("read", 16, 34), ("read", 6, 15), ("write", 16, 19)]
    turn = fit_turnaround(rows, timing)
    assert turn["read"]["cycles"] == 1.0 and turn["read"]["n"] == 3
    assert turn["write"] == {"cycles": 2.0, "n": 1, "spread": [2.0, 2.0]}

    sim = Simulation()
    mem = MemoryMod(name="mem", sim=sim, word_size=64, inline=False, clk=Clock(freq=1.0),
                    nwords_tot=256, calib_dir=tmp_path)
    store = ModuleStore(tmp_path)
    for rec, ident in (mem_timing_record(mem, timing, workload="w"),
                       mem_turnaround_record(mem, turn, workload="t")):
        store.append(rec, identity=ident)
    mem._apply_stored_timing()
    assert mem.s_mm.serialize_transactions
    assert mem.s_mm.turnaround == {"read": 1.0, "write": 2.0}
