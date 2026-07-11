"""Tests for the P-SOB interleaver variant (Phase 4b) — the symmetric topology where BOTH P and X
are resident SOB blocks (Seq -> MemRStream -> SplitFill ->(p_blk,x_blk)-> GatherTwoSob -> MemWStream).

Kept alongside the stream/SOB-mix :class:`Interleaver` (the A/B).  pysim golden bit-exact + the
graph-derived codegen shape: five hls::tasks, four StreamEdges + TWO SobEdges (p_blk, x_blk) — no new
codegen mechanism (composite_gen already handles SobEdge, just two of them).  The csynth + XSI legs
are driven out-of-band by examples/interleaver/interleaver.py generate_sob() + the xsi/ BFM.
"""
from __future__ import annotations

from pathlib import Path


def test_interleaver_sob_pysim_golden():
    """The P-SOB interleaver gathers Y[i]=X[P[i]] bit-exact (single + back-to-back), and the ping-pong
    overlap is visible (steady-state period well under the job-0 fill latency)."""
    from examples.interleaver.interleaver import InterleaverSob
    from examples.interleaver.interleaver_sim import run_interleaver

    for nj in (1, 3, 8):
        il = run_interleaver(nj=nj, comp_class=InterleaverSob)
        assert len(il.gather.job_end_cyc) == nj
    il = run_interleaver(nj=4, comp_class=InterleaverSob)
    done = il.gather.job_end_cyc
    periods = [done[i] - done[i - 1] for i in range(1, len(done))]
    assert all(p < 0.7 * done[0] for p in periods), \
        f"no pipeline overlap: periods {periods} not << fill latency {done[0]}"


def test_interleaver_sob_codegen_shape(tmp_path: Path):
    """The generated P-SOB top: five ap_ctrl_none hls::tasks, four StreamEdges (mr_cmd, mw_cmd,
    mem_out, y_words) + TWO SobEdges (p_blk, x_blk) — no deep p_words FIFO, no new codegen mechanism."""
    from examples.interleaver.interleaver import generate_sob

    generate_sob(out_dir=tmp_path, mem_dwidth=64, n=256)
    src = (tmp_path / "gen" / "interleaver_sob.cpp").read_text()

    assert "#pragma HLS INTERFACE ap_ctrl_none port=return" in src
    assert "while" not in src
    assert "depth=1024" not in src                 # no deep p_words FIFO (P is now resident)

    # two m_axi bundles + two AXIS ports (same boundary as the mix variant).
    assert "#pragma HLS INTERFACE m_axi port=m_in offset=slave bundle=gmem0" in src
    assert "#pragma HLS INTERFACE m_axi port=m_out offset=slave bundle=gmem1" in src
    assert "const ap_uint<64>* m_in" in src

    # TWO stream_of_blocks internal edges (p_blk + x_blk), no p_words stream.
    assert "hls_thread_local hls::stream_of_blocks<ap_uint<64>[128], 2> p_blk;" in src
    assert "hls_thread_local hls::stream_of_blocks<ap_uint<64>[128], 2> x_blk;" in src
    assert "hls::stream<ap_uint<64> > p_words;" not in src
    # four plain stream edges.
    for fifo in ("mr_cmd", "mw_cmd", "mem_out", "y_words"):
        assert f"hls_thread_local hls::stream<ap_uint<64> > {fifo};" in src

    # five tasks, wired as the graph specifies (Demux+Fill merged into split_fill).
    assert "hls::task t0(interleaver_seq_task<64, 128>, s_cmd, mr_cmd, mw_cmd);" in src
    assert "hls::task t1(mem_r_stream_task<64>, mr_cmd, m_in, mem_out);" in src
    assert "hls::task t2(split_fill_task<64, 128>, mem_out, p_blk, x_blk);" in src
    assert "hls::task t3(gather_two_sob_task<64, 128>, p_blk, x_blk, y_words);" in src
    assert "hls::task t4(mem_w_stream_done_task<64>, mw_cmd, y_words, m_out, s_done);" in src

    for h in ("split_fill_task.h", "gather_two_sob_task.h", "il_elem_array_utils.h"):
        assert (tmp_path / "include" / h).exists(), h


def test_interleaver_sob_two_sobedges():
    """The composite graph has exactly two SobEdges (p_blk, x_blk) and no deep StreamEdge — the whole
    point of the symmetric variant; composite_top_spec derives it with no new mechanism."""
    from waveflow.simulation.simulation import Simulation
    from examples.interleaver.composite_gen import SobEdge, composite_top_spec
    from examples.interleaver.interleaver import InterleaverSob

    comp = InterleaverSob(name="s", sim=Simulation(), mem_dwidth=64, n=256)
    sob_edges = [e for e in comp.internal_edges if isinstance(e, SobEdge)]
    assert {e.name for e in sob_edges} == {"p_blk", "x_blk"}
    spec = composite_top_spec(comp, width=64)
    assert len(spec.tasks) == 5
    assert sum("stream_of_blocks" in s for s in spec.internal_streams) == 2
