"""Tests for the in-band interleaver — the gather rebuilt on the framework MemRStream/MemWStream.

cmd_rx (framer) → MemRStream(inband) → il_load → il_compute → il_store → MemWStream(inband) → s_done.
No custom mem adaptors and no custom token: cmd_rx frames two reads (X with the descriptor as a header,
then P), the reader relays opaquely, and il_store frames the writer's stream. pysim golden bit-exact;
codegen + XSI are the toolchain follow-up.
"""
from __future__ import annotations


def test_inband_pysim_golden():
    """Gathers Y[i]=X[P[i]] bit-exact across sizes and job counts, one done per job."""
    from examples.interleaver.interleaver_inband import InterleaverInband
    from examples.interleaver.interleaver_sim import run_interleaver

    for nj, n in ((1, 256), (3, 256), (8, 256), (2, 128), (3, 512)):
        il = run_interleaver(nj=nj, n=n, comp_class=InterleaverInband)
        assert len(il.gather.job_end_cyc) == nj


def test_inband_uses_framework_mem_streams():
    """The read/write adaptors are the framework MemRStream/MemWStream, not custom stages."""
    from waveflow.hw.mem_stream import MemRStream, MemWStream
    from waveflow.simulation.simulation import Simulation
    from examples.interleaver.interleaver_inband import InterleaverInband

    il = InterleaverInband(name="il", sim=Simulation(), mem_dwidth=64, n=256)
    assert isinstance(il.rstream, MemRStream) and bool(il.rstream.inband)
    assert isinstance(il.wstream, MemWStream) and bool(il.wstream.inband) and il.wstream.emit_done
    # the read/write bundles come straight off the framework mem-streams
    assert il.m_in is il.rstream.m_mem
    assert il.m_out is il.wstream.m_mem
    assert il.s_done is il.wstream.s_done


def test_inband_graph_shape():
    """Six sub-components wired by three FRAMED mem-stream edges (the reader command in, the reader
    data, the writer command out), two plain descriptor-token edges through the middle, and three
    stream-of-blocks edges (p/x/y_blk) — the in-band shape, mirroring mem_copy's framed FIFOs."""
    from waveflow.build.composite_gen import FramedEdge, SobEdge, StreamEdge
    from waveflow.simulation.simulation import Simulation
    from examples.interleaver.interleaver_inband import InterleaverInband

    il = InterleaverInband(name="il", sim=Simulation(), mem_dwidth=64, n=256)
    assert len(il.sub_comps) == 6
    framed = {e.name for e in il.internal_edges if isinstance(e, FramedEdge)}
    stream = {e.name for e in il.internal_edges if isinstance(e, StreamEdge)}
    sob = {e.name for e in il.internal_edges if isinstance(e, SobEdge)}
    assert framed == {"cmd_rd", "rdata", "wdata"}     # framer→reader, reader→load, store→writer
    assert stream == {"cmd_lc", "cmd_cs"}             # the descriptor forwarded through the middle
    assert sob == {"p_blk", "x_blk", "y_blk"}
    assert all(isinstance(e, (FramedEdge, StreamEdge, SobEdge)) for e in il.internal_edges)
