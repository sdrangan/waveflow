"""Tests for the Phase-3 SOBIF de-risk: the pure-AXIS Fill ->SOBIF-> Gather toy (Gate 3).

Rung-0 (pysim) leg: the SOBIF ping-pong handover + the composite golden — Fill write-lock-fills a
block, Gather read-locks + random-reads it (Y[i]=X[P[i]]) bit-exact, with the depth-2 overlap visible
(total ~ (NJ+1)*N, not NJ*2N).  Plus the graph-derived composite codegen shape (the SobEdge branch:
one hls::stream_of_blocks internal edge).  The csynth + XSI legs of Gate 3 need Vitis/Vivado and are
driven out-of-band by examples/interleaver/sob_toy.py + examples/interleaver/xsi/run.bat.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np


def test_sobif_pingpong_handover():
    """A SOBIF hands a whole block producer->consumer with write_lock/read_lock semantics, and the
    depth-2 free-buffer model lets the producer commit block 1 while the consumer still holds block
    0 (the ping-pong)."""
    import simpy
    from waveflow.hw.clock import Clock
    from waveflow.hw.interface import SobIFMaster, SobIFSlave, StreamOfBlocksIF
    from waveflow.simulation.simulation import Simulation

    sim = Simulation()
    clk = Clock(freq=100e6)
    sob = StreamOfBlocksIF(name="blk", sim=sim, clk=clk, bitwidth=32, block_n=4)
    m = SobIFMaster(name="m", sim=sim, bitwidth=32, block_n=4)
    s = SobIFSlave(name="s", sim=sim, bitwidth=32, block_n=4)
    sob.bind("master", m)
    sob.bind("slave", s)

    seen: list = []
    inflight: list[int] = []

    def producer():
        for j in range(3):
            buf = yield from m.acquire_write()
            inflight.append(sob.depth - sob._free.level)   # buffers in flight when acquired
            buf[:] = np.arange(4, dtype=np.uint32) + j * 10
            yield from m.commit_write(buf)

    def consumer():
        for _ in range(3):
            yield sim.env.timeout(5)                        # lag the consumer so blocks queue
            blk = yield from s.acquire_read()
            seen.append(np.array(blk, copy=True))
            yield from s.release_read()

    sim.env.process(producer())
    sim.env.process(consumer())
    sim.run_sim()

    assert len(seen) == 3
    for j, blk in enumerate(seen):
        assert np.array_equal(blk, np.arange(4, dtype=np.uint32) + j * 10)
    assert max(inflight) >= 2, "depth-2 ping-pong never had 2 buffers in flight"


def test_sob_toy_pysim_golden():
    """SobToy gathers Y[j][i]=X[j][P[i]] bit-exact with the ping-pong overlap visible in the timeline
    (run_sob asserts internally on both correctness and overlap)."""
    from examples.interleaver.sob_toy_sim import run_sob
    for nj in (4, 1, 3):
        toy = run_sob(nj=nj)
        assert len(toy.gather.job_end_cyc) == nj


def test_sob_toy_codegen_shape(tmp_path: Path):
    """The generated pure-AXIS top instantiates fill_task/gather_task wired by ONE
    hls::stream_of_blocks internal edge (the SobEdge branch) — ap_ctrl_none, no m_axi, no while."""
    from examples.interleaver.sob_toy import generate

    generate(out_dir=tmp_path, elem_bw=32, block_n=256)
    src = (tmp_path / "gen" / "sob_toy.cpp").read_text()

    assert "#pragma HLS INTERFACE ap_ctrl_none port=return" in src
    assert "m_axi" not in src                         # pure-AXIS: no memory ports
    assert "while" not in src
    for port in ("x_in", "p_in", "y_out"):
        assert f"#pragma HLS INTERFACE axis port={port}" in src
    # the one SOB edge lowers to a depth-2 stream_of_blocks (NOT a plain hls::stream).
    assert "hls_thread_local hls::stream_of_blocks<ap_uint<32>[256], 2> x_blk;" in src
    # two compute tiles at concrete <EW, N>, wired exactly as the graph specifies.
    assert "hls_thread_local hls::task t0(fill_task<32, 256>, x_in, x_blk);" in src
    assert "hls_thread_local hls::task t1(gather_task<32, 256>, p_in, x_blk, y_out);" in src
    for h in ("fill_task.h", "gather_task.h"):
        assert (tmp_path / "include" / h).exists(), h


def test_sob_edge_lowering_is_graph_derived():
    """The SobEdge lowers to stream_of_blocks while StreamEdge lowers to hls::stream — the composite
    generator picks the channel decl off the edge kind, everything else unchanged."""
    from examples.interleaver.composite_gen import SobEdge, StreamEdge, composite_top_spec
    from waveflow.simulation.simulation import Simulation
    from examples.interleaver.sob_toy import SobToy

    assert "hls::stream<ap_uint<32> >" in StreamEdge("e", None, None).decl(32)
    assert "stream_of_blocks<ap_uint<32>[256], 2>" in SobEdge("e", None, None, 32, 256).decl(32)

    toy = SobToy(name="t", sim=Simulation(), elem_bw=32, block_n=256)
    spec = composite_top_spec(toy, width=32)
    assert len(spec.tasks) == 2
    assert any("stream_of_blocks" in s for s in spec.internal_streams)
    g_task = next(t for t in spec.tasks if t.task_fn == "gather_task")
    assert g_task.args == ("p_in", "x_blk", "y_out")
    assert g_task.template_args == (32, 256)
