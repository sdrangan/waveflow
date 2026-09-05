"""tests/hw/test_stream_depth.py — a StreamIF's depth is a single-source physical property.

One number feeds both backends: pysim's slave queue_size (bounded at the real FIFO depth, so it
backpressures faithfully) and codegen's `#pragma HLS STREAM depth`.  `None` is explicit-unbounded —
allowed for pysim exploration, rejected when a synthesizable edge lowers.
"""
from __future__ import annotations

import math

import pytest

from waveflow.build.composite_gen import FramedEdge, StreamEdge, derive_internal_edges
from waveflow.build.hwcodegen import SynthesisError
from waveflow.hw.clock import Clock
from waveflow.hw.interface import (
    DEFAULT_STREAM_DEPTH,
    StreamIF,
    StreamIFMaster,
    StreamIFSlave,
)
from waveflow.simulation.simulation import Simulation


def _bound_if(depth="default", slave_queue=None):
    sim = Simulation()
    kw = {} if depth == "default" else {"depth": depth}
    iface = StreamIF(name="ch", sim=sim, bitwidth=64, clk=Clock(freq=100e6), **kw)
    m = StreamIFMaster(name="m", sim=sim, bitwidth=64, has_tlast=False)
    s = StreamIFSlave(name="s", sim=sim, bitwidth=64, has_tlast=False, queue_size=slave_queue)
    iface.bind("master", m)
    iface.bind("slave", s)
    return iface, s


class TestPysimReadsDepth:
    def test_unset_slave_gets_the_channel_depth(self):
        _, s = _bound_if()
        assert s.queue_size == DEFAULT_STREAM_DEPTH
        assert s.nrx.capacity == DEFAULT_STREAM_DEPTH

    def test_explicit_depth_is_applied(self):
        _, s = _bound_if(depth=1024)
        assert s.queue_size == 1024 and s.nrx.capacity == 1024

    def test_endpoint_queue_size_wins(self):
        """A testbench sink that declared its own buffering keeps it — the channel depth applies
        only when the endpoint left it unset."""
        _, s = _bound_if(depth=2, slave_queue=64)
        assert s.queue_size == 64 and s.nrx.capacity == 64

    def test_explicit_unbounded_leaves_the_slave_unbounded(self):
        import math
        _, s = _bound_if(depth=None)
        assert s.queue_size is None and s.nrx.capacity == math.inf


class TestABurstWriteWaitsForRoom:
    """``write()`` blocks until the burst can be admitted — ``plans/pysim_burst_backpressure.md`` S2.

    Before S2 the write path blocked for exactly **one** word and dumped the remainder into the
    unbounded ``ntx``, so a producer was back-pressured almost not at all and ``write()`` behaved
    like ``offer()``.  These are the tests that say the difference exists.
    """

    @staticmethod
    def _run(depth, burst, rx_delay, n_writes=3, until=10_000.0):
        """Drive *n_writes* bursts through one channel; return the sim time each ``write()`` ended.

        The transfer itself costs ``burst`` cycles at 1 Hz, so with an idle consumer the writes end
        at ``burst, 2*burst, 3*burst``.  Anything later than that is a **stall**.
        """
        import numpy as np

        sim = Simulation()
        env = sim.env
        iface = StreamIF(name="ch", sim=sim, bitwidth=64, clk=Clock(freq=1.0), depth=depth)
        received = []

        def rx(words):
            yield env.timeout(rx_delay)
            received.append(len(words))

        m = StreamIFMaster(name="m", sim=sim, bitwidth=64, has_tlast=False)
        sl = StreamIFSlave(name="s", sim=sim, bitwidth=64, has_tlast=False, rx_proc=rx)
        iface.bind("master", m)
        iface.bind("slave", sl)

        ends = []

        def producer():
            for _ in range(n_writes):
                yield from m.write(np.arange(burst, dtype=np.uint64))
                ends.append(env.now)

        env.process(producer())
        env.process(sl.run_proc())
        env.run(until=until)
        return ends, received, sl

    def test_a_producer_into_a_full_channel_actually_stalls(self):
        """**The property this whole arc exists to give the simulator.**

        A 4-word burst into a 4-deep channel fits, so it is one event — but the consumer holds each
        burst for 10 cycles, so by the third write the queue is still occupied and the producer has
        to wait.  Without the change the third write would finish at 12.0 like a free run.
        """
        slow, _, _ = self._run(depth=4, burst=4, rx_delay=10.0)
        free, _, _ = self._run(depth=4, burst=4, rx_delay=0.0)
        assert free == [4.0, 8.0, 12.0], (
            f"an idle consumer should cost only transfer time, got {free}")
        assert slow == [4.0, 8.0, 14.0], (
            f"a slow consumer should stall the producer, got {slow}. If this equals {free} the "
            f"write path is not waiting and S2 has been undone.")
        assert slow[-1] > free[-1], "the stall is the whole point"

    def test_a_burst_larger_than_the_channel_does_not_hang(self):
        """``N > depth`` — the case a bare ``put(N)`` can never satisfy.

        ``simpy.Container.put(n)`` blocks until the whole amount fits, so ``put(8)`` into a 2-deep
        container never completes; and chunking at capacity **deadlocks against this consumer**,
        which takes the whole burst from ``data_buffer`` before retiring a single word.  So the
        reservation is capped at ``min(N, capacity)`` and the remainder is accounted in ``ntx``.

        A hang here looks exactly like a slow test, which is why the assertion is on completion.
        """
        ends, received, sl = self._run(depth=2, burst=8, rx_delay=1.0)
        assert len(ends) == 3, (
            f"only {len(ends)} of 3 writes completed — a burst larger than its channel hung. "
            f"That is the deadlock chunk-at-capacity produces.")
        assert received == [8, 8, 8], f"the consumer got {received}, expected three whole bursts"
        assert sl.nrx.level == 0 and sl.ntx.level == 0, (
            f"accounting left over: nrx={sl.nrx.level} ntx={sl.ntx.level}. The overflow parked in "
            f"ntx must be retired by the read side, which takes from ntx before nrx.")

    def test_an_unbounded_channel_still_works_and_never_stalls(self):
        """``capacity == inf`` — a `CrossBarIF` endpoint declares no ``queue_size``.

        An unbounded container cannot block, so every write costs only its transfer time however
        slow the consumer is.  ``min(N, inf)`` is ``N``, so this needs no special case — but a chunk
        loop against ``inf`` would never terminate, which is why there is no chunk loop.
        """
        ends, _, sl = self._run(depth=None, burst=8, rx_delay=100.0)
        assert sl.nrx.capacity == math.inf, "the channel was supposed to be unbounded"
        assert ends == [8.0, 16.0, 24.0], (
            f"an unbounded channel stalled its producer: {ends}. It cannot; something is chunking "
            f"against inf or applying a bound that is not there.")


class TestSynthesisRejectsUnbounded:
    def _comp_with_edge(self, depth):
        """A tiny composite: two leaves wired by one StreamIF at *depth*."""
        from dataclasses import dataclass
        from waveflow.hw.hw_freerun import FreeRunMod

        @dataclass
        class Prod(FreeRunMod):
            def __post_init__(self):
                super().__post_init__()
                self.out = StreamIFMaster(name=f"{self.name}_out", sim=self.sim, bitwidth=64,
                                          has_tlast=False)
                self.add_endpoint(self.out)

            def run_iter(self):
                yield self.timeout(1)

        @dataclass
        class Cons(FreeRunMod):
            def __post_init__(self):
                super().__post_init__()
                self.inp = StreamIFSlave(name=f"{self.name}_in", sim=self.sim, bitwidth=64,
                                         has_tlast=False)
                self.add_endpoint(self.inp)

            def run_iter(self):
                yield self.timeout(1)

        @dataclass
        class Top(FreeRunMod):
            def __post_init__(self):
                super().__post_init__()
                self.p = Prod(name=f"{self.name}_p", sim=self.sim)
                self.c = Cons(name=f"{self.name}_c", sim=self.sim)
                self.add_comp(self.p)
                self.add_comp(self.c)
                kw = {} if depth == "default" else {"depth": depth}
                iface = StreamIF(name=f"{self.name}_e", sim=self.sim, bitwidth=64,
                                 clk=Clock(freq=100e6), **kw)
                iface.bind("master", self.p.out)
                iface.bind("slave", self.c.inp)
                self.add_if(iface)

        return Top(name="t", sim=Simulation())

    def test_default_depth_lowers_to_an_edge(self):
        edges = derive_internal_edges(self._comp_with_edge("default"))
        assert len(edges) == 1 and edges[0].depth == DEFAULT_STREAM_DEPTH

    def test_unbounded_internal_edge_is_a_synthesis_error(self):
        with pytest.raises(SynthesisError, match="unbounded"):
            derive_internal_edges(self._comp_with_edge(None))


class TestCodegenEmitsOnlyNonDefault:
    def test_default_depth_emits_no_pragma(self):
        """The HLS default IS DEFAULT_STREAM_DEPTH, so a pragma for it would only churn the C++ for
        identical RTL."""
        assert "#pragma HLS STREAM" not in StreamEdge("x", None, None, depth=DEFAULT_STREAM_DEPTH).decl(64)
        assert "#pragma HLS STREAM" not in FramedEdge("x", None, None, depth=DEFAULT_STREAM_DEPTH).decl(64)

    def test_non_default_depth_emits_the_pragma(self):
        d = StreamEdge("x", None, None, depth=1024).decl(64)
        assert "#pragma HLS STREAM variable=x depth=1024" in d


class TestMemCopyChannelsAreBounded:
    def test_internal_framed_edges_bound_to_two_in_pysim(self):
        """The whole point: mem_copy's cmd / copy_data slaves are now depth-2, matching the RTL
        fifo_w65_d2 -- so pysim backpressures where the RTL does."""
        from examples.mem_copy.mem_copy import MemCopy

        dut = MemCopy(name="mc", sim=Simulation(), mem_dwidth=64)
        assert dut.rstream.s_cmd.queue_size == DEFAULT_STREAM_DEPTH   # cmd edge slave
        assert dut.wstream.s_in.queue_size == DEFAULT_STREAM_DEPTH    # copy_data edge slave
