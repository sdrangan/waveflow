"""tests/hw/test_stream_depth.py — a StreamIF's depth is a single-source physical property.

One number feeds both backends: pysim's slave queue_size (bounded at the real FIFO depth, so it
backpressures faithfully) and codegen's `#pragma HLS STREAM depth`.  `None` is explicit-unbounded —
allowed for pysim exploration, rejected when a synthesizable edge lowers.
"""
from __future__ import annotations

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


class TestSynthesisRejectsUnbounded:
    def _comp_with_edge(self, depth):
        """A tiny composite: two leaves wired by one StreamIF at *depth*."""
        from dataclasses import dataclass
        from waveflow.hw.hw_freerun import FreeRunComp

        @dataclass
        class Prod(FreeRunComp):
            def __post_init__(self):
                super().__post_init__()
                self.out = StreamIFMaster(name=f"{self.name}_out", sim=self.sim, bitwidth=64,
                                          has_tlast=False)
                self.add_endpoint(self.out)

            def run_iter(self):
                yield self.timeout(1)

        @dataclass
        class Cons(FreeRunComp):
            def __post_init__(self):
                super().__post_init__()
                self.inp = StreamIFSlave(name=f"{self.name}_in", sim=self.sim, bitwidth=64,
                                         has_tlast=False)
                self.add_endpoint(self.inp)

            def run_iter(self):
                yield self.timeout(1)

        @dataclass
        class Top(FreeRunComp):
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
