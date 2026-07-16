"""Tests for the canonical six-stage interleaver (Phase 4c) — the teachable load-compute-store anatomy
with a forwarded per-job token threaded through every stage.

cmd_rx -> il_mem_r -> il_load -> il_compute -> il_store -> il_mem_w -> s_done.  The token ({n, p_off,
x_off, y_off}) is emitted once per job and read-then-forwarded by every tile (five Cmd StreamEdges),
pacing each tile to one job in flight (sob3's structure) — the predicted nj=8 deadlock fix.  Kept
alongside the intact mix / P-SOB variants (the A/B).  pysim golden bit-exact + the graph-derived
codegen shape (six tasks; only StreamEdge + SobEdge, no new mechanism).  The csynth + XSI legs are
driven out-of-band by examples/interleaver/interleaver.py generate_canon() + the xsi/ BFM.
"""
from __future__ import annotations

from pathlib import Path


def test_interleaver_canon_pysim_golden():
    """The canonical interleaver gathers Y[i]=X[P[i]] bit-exact (single + back-to-back + nj=8), and
    completes all nj jobs in pysim (done == nj — the token pacing keeps the pipeline flowing)."""
    from examples.interleaver.interleaver import InterleaverCanon
    from examples.interleaver.interleaver_sim import run_interleaver

    for nj in (1, 3, 8):
        il = run_interleaver(nj=nj, comp_class=InterleaverCanon)
        assert len(il.gather.job_end_cyc) == nj


def test_interleaver_canon_codegen_shape(tmp_path: Path):
    """The generated top is a free-running ap_ctrl_none top with SIX tasks, the token threaded through
    five Cmd StreamEdges (cmd0..cmd4), three data StreamEdges, and three SobEdges — using only
    StreamEdge + SobEdge (no new codegen mechanism)."""
    from examples.interleaver.interleaver import generate_canon

    generate_canon(out_dir=tmp_path, mem_dwidth=64, n=256)
    src = (tmp_path / "gen" / "interleaver_canon.cpp").read_text()

    assert "#pragma HLS INTERFACE ap_ctrl_none port=return" in src
    assert "while" not in src
    assert "#pragma HLS INTERFACE m_axi port=m_in offset=slave bundle=gmem0" in src
    assert "#pragma HLS INTERFACE m_axi port=m_out offset=slave bundle=gmem1" in src

    # five forwarded-token edges + three data edges (all plain streams).
    for fifo in ("cmd0", "cmd1", "cmd2", "cmd3", "cmd4", "pwords", "xwords", "ywords"):
        assert f"hls_thread_local hls::stream<ap_uint<64> > {fifo};" in src
    # three block edges (p_blk, x_blk, y_blk).
    for blk in ("p_blk", "x_blk", "y_blk"):
        assert f"hls_thread_local hls::stream_of_blocks<ap_uint<64>[128], 2> {blk};" in src

    # six tasks, token threaded rx -> memr -> load -> compute -> store -> memw.
    assert "hls::task t0(cmd_rx_task<64>, s_cmd, cmd0);" in src
    assert "hls::task t1(il_mem_r_task<64, 128>, cmd0, m_in, cmd1, pwords, xwords);" in src
    assert "hls::task t2(il_load_task<64, 128>, cmd1, pwords, xwords, cmd2, p_blk, x_blk);" in src
    assert "hls::task t3(il_compute_task<64, 128>, cmd2, p_blk, x_blk, cmd3, y_blk);" in src
    assert "hls::task t4(il_store_task<64, 128>, cmd3, y_blk, cmd4, ywords);" in src
    assert "hls::task t5(il_mem_w_task<64, 128>, cmd4, ywords, m_out, s_done);" in src

    for h in ("cmd_rx_task.h", "il_mem_r_task.h", "il_load_task.h", "il_compute_task.h",
              "il_store_task.h", "il_mem_w_task.h", "il_elem_array_utils.h"):
        assert (tmp_path / "include" / h).exists(), h


def test_interleaver_canon_token_edges():
    """The graph forwards the token through exactly five Cmd StreamEdges + three SobEdges, using only
    StreamEdge + SobEdge (composite_top_spec derives the six-task top with no generator change)."""
    from waveflow.simulation.simulation import Simulation
    from waveflow.build.composite_gen import SobEdge, StreamEdge, composite_top_spec
    from examples.interleaver.interleaver import InterleaverCanon

    comp = InterleaverCanon(name="c", sim=Simulation(), mem_dwidth=64, n=256)
    stream_edges = [e for e in comp.internal_edges if isinstance(e, StreamEdge)]
    sob_edges = [e for e in comp.internal_edges if isinstance(e, SobEdge)]
    assert {e.name for e in sob_edges} == {"p_blk", "x_blk", "y_blk"}
    assert {"cmd0", "cmd1", "cmd2", "cmd3", "cmd4"} <= {e.name for e in stream_edges}
    # every edge is a StreamEdge or SobEdge — no other kind.
    assert all(isinstance(e, (StreamEdge, SobEdge)) for e in comp.internal_edges)

    spec = composite_top_spec(comp, width=64)
    assert len(spec.tasks) == 6
    assert sum("stream_of_blocks" in s for s in spec.internal_streams) == 3
