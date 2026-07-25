"""tests/build/test_int_channel.py -- the internal-edge half of the TopSpec contract.

``composite_top_spec`` used to keep only ``edge.decl(width)`` for each internal edge, so the spec
could emit a channel but could not answer anything about it: the name was recoverable only by
parsing C++ back out of a string, and the endpoints were gone entirely.  :class:`IntChannel` gives
internal edges the same treatment :class:`ExtPort` already gives boundary ports.

Why it matters beyond tidiness: a channel's RTL nets are derived from these fields.  A channel
``cmd`` between two ``hls::task`` bodies lowers to top-scope ``cmd_dout`` / ``cmd_empty_n`` /
``cmd_full_n`` plus ``<producer_inst>_cmd_din`` / ``_cmd_write`` and ``<consumer_inst>_cmd_read``,
where ``<inst>`` is ``<task_fn>_<template args>_U0``.  Those names have been checked against the
real csynth RTL for both designs below (9 task instances, 10 FIFO channels, 0 mismatches); these
tests pin the Python side so a codegen change cannot silently move them.

Everything here is elaborate-time only -- no Vitis, no RTL, no simulation.
"""
from __future__ import annotations

import pytest

from waveflow.build.composite_gen import IntChannel, SobEdge, composite_top_spec
from waveflow.simulation.simulation import Simulation


def _inst(task) -> str:
    """The RTL instance name a TaskInst becomes.  Only the ``_U0`` suffix is Vitis's choice."""
    return "_".join([task.task_fn] + [str(a) for a in task.template_args]) + "_U0"


@pytest.fixture
def memcopy_spec():
    from examples.mem_copy.mem_copy import MemCopy
    return composite_top_spec(MemCopy(name="mc", sim=Simulation(), mem_dwidth=64), width=64)


@pytest.fixture
def interleaver_spec():
    from examples.interleaver.interleaver_inband import InterleaverInband
    return composite_top_spec(
        InterleaverInband(name="c", sim=Simulation(), mem_dwidth=64, n=256), width=64)


class TestChannelsAreAnswerable:
    def test_memcopy_channels_carry_name_kind_and_width(self, memcopy_spec):
        chans = {c.name: c for c in memcopy_spec.channels}
        assert set(chans) == {"cmd", "copy_data"}
        for c in chans.values():
            assert c.kind == "framed", "both memcpy edges are framed StreamIFs"
            assert c.width == 64, "the PAYLOAD width; the RTL net is 65 bits with `last` on top"

    def test_channel_kind_comes_from_the_edge_type(self, interleaver_spec):
        # The kind is read off the edge TYPE: a framed StreamIF -> framed_word FIFO, a SobIF ->
        # stream_of_blocks.  (Every in-band internal edge is framed; plain ap_uint FIFOs are legacy.)
        kinds = {c.name: c.kind for c in interleaver_spec.channels}
        assert kinds["desc_lc"] == "framed", "a framed StreamIF lowers to a framed_word FIFO"
        assert kinds["p_blk"] == "sob", "a SobIF lowers to a stream_of_blocks"

    def test_sob_channel_carries_its_element_width_not_the_bus_width(self):
        """A stream_of_blocks is templated on the ELEMENT type, so its width is the edge's own."""
        from examples.interleaver.interleaver_inband import InterleaverInband

        comp = InterleaverInband(name="c", sim=Simulation(), mem_dwidth=64, n=256)
        spec = composite_top_spec(comp, width=64)
        elem_bw = {e.name: e.elem_bw for e in comp.internal_edges if isinstance(e, SobEdge)}

        sob = [c for c in spec.channels if c.kind == "sob"]
        assert len(sob) == 3, "the interleaver has three stream_of_blocks edges"
        for c in sob:
            assert c.width == elem_bw[c.name]


class TestChannelsKnowTheirEndpoints:
    def test_memcopy_chain_is_recoverable(self, memcopy_spec):
        """Sequencer -> MemRStream -> MemWStream, read off the spec rather than the waveform."""
        by_name = {c.name: c for c in memcopy_spec.channels}
        fn = lambda i: memcopy_spec.tasks[i].task_fn           # noqa: E731

        assert fn(by_name["cmd"].master_task) == "mem_seq_framed_task"
        assert fn(by_name["cmd"].slave_task) == "mem_r_stream_framed_task"
        assert fn(by_name["copy_data"].master_task) == "mem_r_stream_framed_task"
        assert fn(by_name["copy_data"].slave_task) == "mem_w_stream_framed_done_task"

    def test_interleaver_control_chain_is_recoverable(self, interleaver_spec):
        """The 6-stage pipeline, derived from Python -- it matches the order observed in the VCD.
        Every internal edge is framed (the in-band descriptor rides them)."""
        by_name = {c.name: c for c in interleaver_spec.channels}
        fn = lambda i: interleaver_spec.tasks[i].task_fn       # noqa: E731

        chain = [(fn(by_name[e].master_task), fn(by_name[e].slave_task))
                 for e in ("cmd_rd", "rdata", "desc_lc", "desc_cs", "wdata")]
        assert chain == [
            ("il_cmd_rx_framed_task", "mem_r_stream_framed_task"),
            ("mem_r_stream_framed_task", "il_load_inband_task"),
            ("il_load_inband_task", "il_compute_inband_task"),
            ("il_compute_inband_task", "il_store_inband_task"),
            ("il_store_inband_task", "mem_w_stream_framed_done_task"),
        ]

    def test_every_channel_has_both_endpoints_resolved(self, interleaver_spec):
        """An unresolved endpoint would silently produce a channel whose nets cannot be named."""
        for c in interleaver_spec.channels:
            assert c.master_task is not None, f"{c.name} has no producer task"
            assert c.slave_task is not None, f"{c.name} has no consumer task"
            assert 0 <= c.master_task < len(interleaver_spec.tasks)
            assert 0 <= c.slave_task < len(interleaver_spec.tasks)

    def test_predicted_rtl_instance_names(self, memcopy_spec):
        """Pinned against the names actually present in mem_copy's csynth RTL."""
        assert {_inst(t) for t in memcopy_spec.tasks} == {
            "mem_seq_framed_task_64_U0",
            "mem_r_stream_framed_task_64_U0",
            "mem_w_stream_framed_done_task_64_8_U0",
        }


class TestInternalStreamsStaysDerived:
    def test_decls_are_the_channels_decls(self, interleaver_spec):
        """`internal_streams` is now a view, so a decl cannot drift from its channel."""
        assert interleaver_spec.internal_streams == tuple(
            c.decl for c in interleaver_spec.channels)

    def test_decl_content_is_unchanged(self, memcopy_spec, interleaver_spec):
        assert len(memcopy_spec.internal_streams) == 2
        assert all("framed_word<64>" in d for d in memcopy_spec.internal_streams)
        assert sum("stream_of_blocks" in s for s in interleaver_spec.internal_streams) == 3

    def test_a_leaf_has_no_channels(self):
        """A standalone kernel wires nothing -- every port is a boundary port."""
        from waveflow.hw.mem_stream import MemRStream
        leaf = MemRStream(name="mem_r_stream", sim=Simulation(), mem_dwidth=64)
        leaf.cmd_headers = ()
        spec = composite_top_spec(leaf, width=64)
        assert spec.channels == ()
        assert spec.internal_streams == ()

    def test_default_is_empty(self):
        from waveflow.build.composite_gen import TopSpec
        spec = TopSpec(top_name="k", ports=(), tasks=(), cmd_headers=())
        assert spec.channels == ()
        assert spec.internal_streams == ()


def test_intchannel_defaults_are_inert():
    """Constructed with a decl alone it must still be a valid (if uninformative) channel."""
    c = IntChannel(decl="hls_thread_local hls::stream<ap_uint<64> > x;")
    assert c.name == "" and c.kind == "" and c.width == 0
    assert c.master_task is None and c.slave_task is None
