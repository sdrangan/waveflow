"""A CreditStreamIF routed over a bus (``plans/mm_credit_stream.md`` Stage 1).

One producer and one consumer, run twice: joined directly by a ``CreditStreamIF``, and routed over a
crossbar by an ``MmCreditStreamIF`` (producer's writer -> consumer's queue-in view; consumer's
writer -> producer's credit-in view).  The kernels are the same objects with the same code; only the
wiring differs.  The routed run must deliver the same words, never stall the bus, and send about one
credit write per ``crd_every`` words.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar

import numpy as np
import pytest

from waveflow.hw.clock import Clock
from waveflow.hw.hw_module import HwModule
from waveflow.hw.memif import AXIMMCrossBarIF, assign_address_ranges
from waveflow.hw.mm_credit import MemSlaveCreditIn, MmCreditStreamIF
from waveflow.hw.mm_device import CreditIn, QueueIn, build_mm_device, layout_of
from waveflow.hw.reverse_stream import CreditStreamIF, CreditStreamMasterIF, CreditStreamSlaveIF
from waveflow.simulation.simulation import Simulation

DW = 64
QDEPTH = 16
CRD_EVERY = QDEPTH // 2


@dataclass
class Prod(HwModule):
    mm_views: ClassVar[tuple] = (CreditIn("crd", port="m_out"),)
    nwords: int = 200
    burst: int = 5
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        self.m_out = CreditStreamMasterIF(name=f"{self.name}_m_out", sim=self.sim, bitwidth=DW)
        self.add_endpoint(self.m_out)

    def run_proc(self):
        for k in range(0, self.nwords, self.burst):
            yield from self.m_out.write(np.arange(k, k + self.burst, dtype=np.uint64))


@dataclass
class Cons(HwModule):
    mm_views: ClassVar[tuple] = (QueueIn("qin", port="s_in", depth=QDEPTH),)
    nwords: int = 200
    #: Cycles per word: slower than the producer, so the queue fills and credit gates the producer.
    cycles_per_word: int = 3
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        self.s_in = CreditStreamSlaveIF(name=f"{self.name}_s_in", sim=self.sim, bitwidth=DW,
                                        crd_every=CRD_EVERY)
        self.add_endpoint(self.s_in)
        self.got: list[int] = []
        self.done = self.env.event()

    def run_proc(self):
        while len(self.got) < self.nwords:
            d = yield from self.s_in.get()
            yield self.timeout(len(d) * self.cycles_per_word * self.clk.period)
            self.got.extend(int(v) for v in np.asarray(d).reshape(-1))
        self.done.succeed()


def _direct(nwords=200, burst=5):
    sim, clk = Simulation(), Clock(freq=100e6)
    p = Prod(name="p", sim=sim, nwords=nwords, burst=burst, clk=clk)
    c = Cons(name="c", sim=sim, nwords=nwords, clk=clk)
    ch = CreditStreamIF(name="ch", sim=sim, clk=clk, bitwidth=DW, depth=QDEPTH)
    ch.bind("master", p.m_out)
    ch.bind("slave", c.s_in)
    sim.run_sim(until=c.done)
    return p, c, None, sim


def _routed(nwords=200, burst=5):
    sim, clk = Simulation(), Clock(freq=100e6)
    p = Prod(name="p", sim=sim, nwords=nwords, burst=burst, clk=clk)
    c = Cons(name="c", sim=sim, nwords=nwords, clk=clk)
    pdev = build_mm_device(p, sim=sim, clk=clk, mem_dwidth=DW, prefix="p_")
    cdev = build_mm_device(c, sim=sim, clk=clk, mem_dwidth=DW, prefix="c_")
    ch = MmCreditStreamIF(name="ch", sim=sim, clk=clk, bitwidth=DW)
    ch.bind("master", p.m_out)
    ch.bind("slave", c.s_in)
    masters = ch.bus_masters()
    pslaves, prng = pdev.ranges(0x0000)
    cslaves, crng = cdev.ranges(0x4000)
    slaves = pslaves + cslaves
    xbar = AXIMMCrossBarIF(name="xbar", sim=sim, clk=clk, nports_master=len(masters),
                           nports_slave=len(slaves), bitwidth=DW, latency_init=4, latency_travel=2)
    for k, m in enumerate(masters):
        xbar.bind(f"master_{k}", m)
    for k, s in enumerate(slaves):
        xbar.bind(f"slave_{k}", s)
    assign_address_ranges(slaves, prng + crng)
    ch.place(qin=cdev.layout.at(0x4000)["qin"], crd_in=pdev.layout.at(0x0000)["crd"])
    sim.run_sim(until=c.done)
    return p, c, (ch, cdev, pdev), sim


def test_the_layout_carries_the_credit_view():
    lay = layout_of(Prod, mem_dwidth=DW)
    assert lay["crd"].kind == "credit_in" and lay["crd"].base == 0


def test_direct_and_routed_deliver_the_same_words():
    _, cd, _, _ = _direct()
    _, cr, _, _ = _routed()
    assert cd.got == cr.got == list(range(200))


def test_routed_never_stalls_the_bus_and_batches_credit():
    p, c, (ch, cdev, pdev), _ = _routed(nwords=400)
    qin = cdev.views["qin"]
    assert qin.nstall == 0, "a credit-respecting producer must never write into a full queue"
    assert p.m_out.n_credit_waits > 0, "the consumer is slower: the producer must have waited"
    # One offer per CRD_EVERY words, plus drain flushes; each offer is at most one bus write.
    assert c.s_in.n_offers <= 400 // CRD_EVERY + 400 // 5
    assert ch.crd_writer.nwrites <= c.s_in.n_offers
    assert ch.crd_writer.nwrites >= 400 // CRD_EVERY // 2
    # The forward writer: one packet per producer burst (5 words + 1 header, one bus write each).
    assert ch.fwd_writer.nwrites == 400 // 5
    assert qin.packets == [5] * (400 // 5)
    crd = pdev.views["crd"]
    assert crd.nwrites == ch.crd_writer.nwrites and crd.value == c.s_in.consumed


def test_a_credit_in_write_overwrites_and_delivers_the_newest():
    """A credit value written while the kernel has not taken the previous one replaces it: the
    kernel sees fewer values, never an older one, and the bus write does not wait."""
    sim, clk = Simulation(), Clock(freq=100e6)
    v = MemSlaveCreditIn(name="v", sim=sim, clk=clk)
    from waveflow.hw.interface import StreamIF, StreamIFSlave
    k = StreamIFSlave(name="k", sim=sim, bitwidth=16, has_tlast=True)
    si = StreamIF(name="s", sim=sim, clk=clk, bitwidth=16, depth=1)
    si.bind("master", v.m_out)
    si.bind("slave", k)
    seen = []

    def bus():
        for val in (3, 5, 9, 12):
            t0 = sim.env.now
            yield from v._on_write(np.asarray([val], dtype=np.uint64), 0)
            assert sim.env.now == t0, "a credit write never waits"

    def kernel():
        yield sim.env.timeout(1e-6)                      # take nothing until the writes are done
        for _ in range(2):
            d = yield from k.get()
            seen.append(int(np.asarray(d)[-1]))

    sim.env.process(v.run_proc())
    sim.env.process(bus())
    sim.env.process(kernel())
    sim.env.run(until=1e-5)
    assert seen[-1] == 12 and seen == sorted(seen) and len(seen) == 2


def test_place_refuses_the_wrong_views():
    sim, clk = Simulation(), Clock(freq=100e6)
    p = Prod(name="p", sim=sim, clk=clk)
    c = Cons(name="c", sim=sim, clk=clk)
    pdev = build_mm_device(p, sim=sim, clk=clk, mem_dwidth=DW, prefix="p_")
    cdev = build_mm_device(c, sim=sim, clk=clk, mem_dwidth=DW, prefix="c_")
    ch = MmCreditStreamIF(name="ch", sim=sim, clk=clk, bitwidth=DW)
    ch.bind("master", p.m_out)
    ch.bind("slave", c.s_in)
    with pytest.raises(ValueError):
        ch.place(qin=pdev.layout.at(0)["crd"], crd_in=cdev.layout.at(0x4000)["qin"])


def test_binding_before_the_device_is_refused():
    sim, clk = Simulation(), Clock(freq=100e6)
    p = Prod(name="p", sim=sim, clk=clk)
    ch = MmCreditStreamIF(name="ch", sim=sim, clk=clk, bitwidth=DW)
    with pytest.raises(RuntimeError):
        ch.bind("master", p.m_out)


def test_negative_control_a_producer_told_the_wrong_depth_stalls_the_bus(monkeypatch):
    """The gate's zero is not vacuous: tell the producer the queue is 4x deeper than it is, and its
    credit admits packets the queue cannot hold -- ``nstall`` counts them."""
    import dataclasses

    orig = MmCreditStreamIF.place

    def lying_place(self, qin, crd_in):
        orig(self, dataclasses.replace(qin, depth=4 * int(qin.depth)), crd_in)

    monkeypatch.setattr(MmCreditStreamIF, "place", lying_place)
    _, c, (ch, cdev, _), _ = _routed(nwords=400)
    assert c.got == list(range(400))          # pysim's burst-granular stall still delivers ...
    assert cdev.views["qin"].nstall > 0       # ... but it stalled the bus to do it
