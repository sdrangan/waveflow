"""Tests for the full generated interleaver (Phase 4, Gate 4).

Rung-0 (pysim) leg: the complete free-running graph — Sequencer -> MemRStream -> Demux -> Fill
->SOBIF-> GatherWord -> MemWStream — gathers Y[i]=X[P[i]] bit-exact (single + back-to-back jobs),
with the load/ping-pong overlap visible (steady-state period well under the fill latency).  Plus the
graph-derived composite codegen shape: one ap_ctrl_none top, six hls::tasks wired by six StreamEdges +
one SobEdge, two m_axi bundles + two AXIS ports — no new codegen mechanism beyond the P2/P3 seam.  The
csynth + XSI legs of Gate 4 need Vitis/Vivado and are driven out-of-band by
examples/interleaver/interleaver.py + examples/interleaver/xsi/run.bat.
"""
from __future__ import annotations

from pathlib import Path


def test_interleaver_cmd_schema():
    """InterleaverCmd packs {p_off, x_off, y_off, n} — two 64-bit words at MEM_DW=64 (LSB-first)."""
    from examples.interleaver.interleaver import InterleaverCmd

    assert InterleaverCmd.nwords_per_inst(64) == 2
    c = InterleaverCmd(p_off=16, x_off=144, y_off=272, n=256)
    w = c.serialize(word_bw=64)
    assert int(w[0]) == 16 | (144 << 32)           # p_off low, x_off high (word 0)
    assert int(w[1]) == 272 | (256 << 32)          # y_off low, n high (word 1)
    d = InterleaverCmd().deserialize(w, word_bw=64)
    assert (int(d.p_off), int(d.x_off), int(d.y_off), int(d.n)) == (16, 144, 272, 256)


def test_interleaver_pysim_golden():
    """The full interleaver gathers Y[i]=X[P[i]] bit-exact (run asserts internally on mismatch)."""
    from examples.interleaver.interleaver_sim import run_interleaver
    for nj in (1, 2, 3):
        il = run_interleaver(nj=nj)
        assert len(il.gather.job_end_cyc) == nj


def test_interleaver_pipeline_overlap():
    """Back-to-back jobs pipeline: the steady-state period (slope between gather completions) is well
    under the job-0 completion (the fill latency) — the SOBIF ping-pong + free-running load overlap."""
    from examples.interleaver.interleaver_sim import run_interleaver
    il = run_interleaver(nj=4)
    done = il.gather.job_end_cyc
    fill_latency = done[0]
    periods = [done[i] - done[i - 1] for i in range(1, len(done))]
    assert all(p < 0.7 * fill_latency for p in periods), \
        f"no pipeline overlap: periods {periods} not << fill latency {fill_latency}"


def test_interleaver_codegen_shape(tmp_path: Path):
    """The generated top is a free-running ap_ctrl_none top instantiating the six sub-component task
    bodies wired by internal channels derived from the graph — six StreamEdges + one SobEdge, two
    m_axi bundles + two AXIS ports.  No new codegen mechanism (the P2/P3 seam held)."""
    from examples.interleaver.interleaver import generate

    generate(out_dir=tmp_path, mem_dwidth=64, n=256)
    src = (tmp_path / "gen" / "interleaver.cpp").read_text()

    assert "#pragma HLS INTERFACE ap_ctrl_none port=return" in src
    assert "while" not in src

    # boundary: two m_axi bundles (const read gmem0, plain write gmem1) + two AXIS ports.
    assert "#pragma HLS INTERFACE m_axi port=m_in offset=slave bundle=gmem0" in src
    assert "#pragma HLS INTERFACE m_axi port=m_out offset=slave bundle=gmem1" in src
    assert "const ap_uint<64>* m_in" in src
    assert "ap_uint<64>* m_out" in src and "const ap_uint<64>* m_out" not in src
    assert "#pragma HLS INTERFACE axis port=s_cmd" in src
    assert "#pragma HLS INTERFACE axis port=s_done" in src

    # six internal StreamEdges; p_words is the deep FIFO (P loaded first, buffered while X fills).
    for fifo in ("mr_cmd", "mw_cmd", "mem_out", "p_words", "x_words", "y_words"):
        assert f"hls_thread_local hls::stream<ap_uint<64> > {fifo};" in src
    assert "#pragma HLS STREAM variable=p_words depth=1024" in src
    # one SobEdge: the word block ping-pong (elem_bw=MEM_DW=64, block_n=n/LW=128).
    assert "hls_thread_local hls::stream_of_blocks<ap_uint<64>[128], 2> x_blk;" in src

    # six tasks at concrete template args, wired exactly as the graph specifies.
    assert "hls::task t0(interleaver_seq_task<64, 128>, s_cmd, mr_cmd, mw_cmd);" in src
    assert "hls::task t1(mem_r_stream_task<64>, mr_cmd, m_in, mem_out);" in src
    assert "hls::task t2(demux_task<64, 128>, mem_out, p_words, x_words);" in src
    assert "hls::task t3(fill_task<64, 128>, x_words, x_blk);" in src
    assert "hls::task t4(gather_word_task<64, 128>, p_words, x_blk, y_words);" in src
    assert "hls::task t5(mem_w_stream_done_task<64>, mw_cmd, y_words, m_out, s_done);" in src

    # the block element type's array-utils header (elem_read<MEM_DW>) was generated.
    assert (tmp_path / "include" / "il_elem_array_utils.h").exists()
    for h in ("interleaver_seq_task.h", "demux_task.h", "gather_word_task.h", "fill_task.h"):
        assert (tmp_path / "include" / h).exists(), h
