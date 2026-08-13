"""``StreamIFMaster.offer()`` — the non-blocking write, and the drop counter that is the one
mechanically checkable clause of the block-LT fidelity contract.

``write()`` waits for room. That is right for a module — a kernel with nowhere to put its output
stalls, and modelling the stall is what a bounded queue is for. It is wrong for a producer that
physically cannot wait: a data converter presents a beat whether or not the fabric is ready, and
what is not taken is gone.

The difference is a property of the **producer**, not of the wire, so it is a method on the master
rather than a flag or a new interface type. The counter lives on the **interface**, which already
owns the queue and the depth that decide a drop — the same split ``RFSampIF`` uses one rung up
(``put()`` yields, ``deliver()`` does not, the edge counts).

**What these tests pin, and what they cannot.** The rule admits a burst unless the consumer has not
kept up *by the time the producer starts the next one*. That is checked in both directions here: a
consumer that never stalls must lose nothing, and one that genuinely cannot keep up must lose
something. What it cannot see is a consumer that stalls *inside* a block period — see
``docs/guide/rf/fidelity.md`` on where that boundary sits and why it matters.
"""
from __future__ import annotations

import numpy as np
import pytest

from waveflow.hw.clock import Clock
from waveflow.hw.interface import StreamIF, StreamIFMaster, StreamIFSlave
from waveflow.simulation.simulation import Simulation

NWORDS = 64
NBURST = 8


def _run(consumer_stall: float, word_rate: float = 64e6, depth: int | None = None):
    """One producer offering ``NBURST`` bursts into a consumer that stalls for *consumer_stall*."""
    sim = Simulation()
    m = StreamIFMaster(name="m", sim=sim, bitwidth=64, has_tlast=False)
    s = StreamIFSlave(name="s", sim=sim, bitwidth=64, has_tlast=False)
    kw = {} if depth is None else {"depth": depth}
    iface = StreamIF(name="i", sim=sim, clk=Clock(freq=300e6), bitwidth=64, **kw)
    iface.bind("master", m)
    iface.bind("slave", s)

    got: list[int] = []

    def consumer():
        while True:
            w = yield from s.get(nwords_max=NWORDS)
            got.append(len(w))
            if consumer_stall:
                yield sim.env.timeout(consumer_stall)

    def producer():
        for _ in range(NBURST):
            yield from m.offer(np.arange(NWORDS, dtype=np.uint64), word_rate=word_rate)

    sim.env.process(consumer())
    sim.env.process(producer())
    sim.env.run()
    return iface, got


class TestTheCounterIsNonVacuous:
    """A counter that has never counted is not evidence — in either direction."""

    def test_a_consumer_that_never_stalls_loses_nothing(self):
        """The half that makes the clause usable.

        If a compliant consumer still showed drops, ``dropped == 0`` would be unreachable and the
        contract clause worthless. Two earlier candidate rules failed exactly here: clipping a burst
        to the free space reported 504 of 512 words lost, and sampling occupancy without letting the
        current instant settle reported 256 — both purely because a depth-2 queue cannot hold a
        64-word burst, which the framework has never treated as a violation (``_push_to_endpoint``
        routes intra-burst overflow to the unbounded ``ntx``).
        """
        iface, got = _run(consumer_stall=0)
        assert iface.dropped == 0
        assert len(got) == NBURST
        assert all(n == NWORDS for n in got)

    def test_a_consumer_that_cannot_keep_up_loses_words(self):
        """The other half. A 5 us stall against a 1 us block period is a consumer falling behind."""
        iface, got = _run(consumer_stall=5e-6)
        assert iface.dropped > 0
        assert iface.dropped % NWORDS == 0, "a burst is dropped whole, not clipped"
        assert iface.last_drop_time > 0
        assert len(got) < NBURST

    def test_the_loss_grows_with_the_stall(self):
        """The count is a model of the consumer, not a constant that happened to match once."""
        assert _run(consumer_stall=2e-6)[0].dropped < _run(consumer_stall=9e-6)[0].dropped


class TestTheProducersRatePacesTheTransfer:

    def test_a_slower_producer_occupies_the_wire_longer(self):
        """Occupancy is ``nwords / word_rate``, not ``nwords / f_axis``.

        Charging a converter's burst at the fabric clock claims it crosses 4.7x faster than the
        converter can physically produce it, and hands the consumer a hole to drain in that the
        hardware never gives it. Getting the occupancy right is what makes a drop appear at all.
        """
        sim = Simulation()
        m = StreamIFMaster(name="m", sim=sim, bitwidth=64, has_tlast=False)
        s = StreamIFSlave(name="s", sim=sim, bitwidth=64, has_tlast=False)
        iface = StreamIF(name="i", sim=sim, clk=Clock(freq=300e6), bitwidth=64)
        iface.bind("master", m)
        iface.bind("slave", s)
        done: list[float] = []

        def drain():
            while True:
                yield from s.get(nwords_max=NWORDS)

        def producer():
            yield from m.offer(np.arange(NWORDS, dtype=np.uint64), word_rate=64e6)
            done.append(float(sim.env.now))

        sim.env.process(drain())
        sim.env.process(producer())
        sim.env.run()
        assert done[0] == pytest.approx(NWORDS / 64e6)          # 1 us, the converter's own rate
        assert done[0] > NWORDS / 300e6                          # not 213 ns, the fabric's

    def test_the_default_rate_is_the_fabric_clock(self):
        """A module inside the fabric is clocked by the fabric; only a converter passes its own."""
        sim = Simulation()
        m = StreamIFMaster(name="m", sim=sim, bitwidth=64, has_tlast=False)
        s = StreamIFSlave(name="s", sim=sim, bitwidth=64, has_tlast=False)
        iface = StreamIF(name="i", sim=sim, clk=Clock(freq=300e6), bitwidth=64)
        iface.bind("master", m)
        iface.bind("slave", s)
        done: list[float] = []

        def drain():
            while True:
                yield from s.get(nwords_max=NWORDS)

        def producer():
            yield from m.offer(np.arange(NWORDS, dtype=np.uint64))
            done.append(float(sim.env.now))

        sim.env.process(drain())
        sim.env.process(producer())
        sim.env.run()
        assert done[0] == pytest.approx(NWORDS / 300e6)


class TestWriteIsUnchanged:
    """``offer`` is additive: an ordinary module keeps stalling, and keeps losing nothing."""

    def test_write_still_blocks_rather_than_dropping(self):
        sim = Simulation()
        m = StreamIFMaster(name="m", sim=sim, bitwidth=64, has_tlast=False)
        s = StreamIFSlave(name="s", sim=sim, bitwidth=64, has_tlast=False)
        iface = StreamIF(name="i", sim=sim, clk=Clock(freq=300e6), bitwidth=64)
        iface.bind("master", m)
        iface.bind("slave", s)
        got: list[int] = []

        def slow_consumer():
            while True:
                w = yield from s.get(nwords_max=NWORDS)
                got.append(len(w))
                yield sim.env.timeout(5e-6)

        def producer():
            for _ in range(NBURST):
                yield from m.write(np.arange(NWORDS, dtype=np.uint64))

        sim.env.process(slow_consumer())
        sim.env.process(producer())
        sim.env.run()
        assert iface.dropped == 0, "write() waits; it must never drop"
        assert len(got) == NBURST, "every burst arrives, however slowly"
