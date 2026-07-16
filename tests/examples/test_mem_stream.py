"""Tests for the MemRStream / MemWStream memory-endpoint components (Phase 1, Gate 1).

Rung-0 (pysim) leg: the run_proc golden is bit-exact — a MemRStream bursts a memory region onto a
stream, a MemWStream drains a stream into a region.  Plus the schema single-source (the command
struct C++ generates from the DataList) and the template codegen shape.  The csynth + XSI legs of
Gate 1 need Vitis/Vivado and are driven out-of-band by examples/interleaver/mem_stream_gen.py +
examples/interleaver/xsi/run.bat.
"""
from __future__ import annotations

from pathlib import Path


def test_mrcmd_schema_single_source():
    """MRCmd / MWCmd pack {addr, len, xfer_len, xfer_msg[max_xfer_len]} (LSB-first): word0 =
    addr | (len<<32), word1 = xfer_len | (xfer_msg[0]<<32), remaining words carry xfer_msg[1:]."""
    from waveflow.hw.mem_stream import MRCmd, MWCmd
    import numpy as np

    assert MRCmd.nwords_per_inst(64) == 6            # 2 (addr/len, xfer_len/msg[0]) + 4 (msg[1:8])
    assert MRCmd.nwords_per_inst(32) == 11
    xfer_msg = np.zeros(8, dtype=np.uint32)
    xfer_msg[0] = 42
    c = MRCmd(addr=100, len=128, xfer_len=1, xfer_msg=xfer_msg)
    w = c.serialize(word_bw=64)
    assert int(w[0]) == 100 | (128 << 32)          # addr low, len high
    assert int(w[1]) == 1 | (42 << 32)             # xfer_len low, xfer_msg[0] high
    d = MRCmd().deserialize(w, word_bw=64)
    assert int(d.addr) == 100 and int(d.len) == 128 and int(d.xfer_len) == 1
    assert int(np.array(d.xfer_msg)[0]) == 42
    # mirror schema
    assert MWCmd.nwords_per_inst(64) == 6


def test_mem_r_stream_pysim_golden():
    """MemRStream bursts a known memory region onto m_out bit-exact (run_read asserts internally
    on mismatch, so a returned component == the golden held)."""
    from examples.interleaver.mem_stream_sim import run_read
    for nw in (128, 1, 257):                       # 1 = single-word; 257 = multiple AXI bursts
        assert run_read(n_words=nw).transfer_spans   # ran, recorded a span


def test_mem_w_stream_pysim_golden():
    """MemWStream drains a known stream into a memory region bit-exact."""
    from examples.interleaver.mem_stream_sim import run_write
    for nw in (128, 1, 257):
        assert run_write(n_words=nw).transfer_spans


def test_mem_stream_word_addressed():
    """The command is an element/word coordinate, so a **word-addressed** memory (byte_addressable
    =False) takes the *identical* word_index command a byte-addressed one does and reads/writes the
    same words — proving the arena convention (Region._word_bytes absorbs the address unit)."""
    from examples.interleaver.mem_stream_sim import run_read, run_write
    for nw in (128, 1, 257):
        assert run_read(n_words=nw, byte_addressable=False).transfer_spans
        assert run_write(n_words=nw, byte_addressable=False).transfer_spans


def test_mem_stream_read_write_overlap():
    """The a2s/s2a read+write is modeled as OVERLAPPED (~n_words + fill), not the ~2*n_words of a
    sequential read-then-write — the Region.read_slice_pipelined + write_pipelined early-anchor."""
    from examples.interleaver.mem_stream_sim import run_read, run_write, overlap_span_cyc

    nw = 128
    r_span = overlap_span_cyc(run_read(n_words=nw))
    w_span = overlap_span_cyc(run_write(n_words=nw))
    # Overlap: span is close to n_words (+small fill), well under the 2*n_words sequential cost.
    assert nw <= r_span < 1.5 * nw, f"read span {r_span} not overlapped (~{nw})"
    assert w_span < 1.5 * nw, f"write span {w_span} not overlapped (~{nw})"


def test_mem_stream_codegen_shape(tmp_path: Path):
    """The generated top is a free-running ap_ctrl_none hls::task top that bakes a concrete width
    and instantiates the copied template body — no #define MEM_DW, no while, and the read owner's
    m_mem is a const pointer (the write owner's is plain)."""
    from examples.interleaver.mem_stream_gen import generate

    generate(out_dir=tmp_path, width=64)
    r = (tmp_path / "gen" / "mem_r_stream.cpp").read_text()
    w = (tmp_path / "gen" / "mem_w_stream.cpp").read_text()
    for src in (r, w):
        assert "#pragma HLS INTERFACE ap_ctrl_none port=return" in src
        assert "hls_thread_local hls::task" in src
        assert "#define MEM_DW" not in src          # concrete width baked in, not a macro
        assert "while" not in src                    # single-firing; the hls::task runtime re-fires
    # concrete-width template instantiation of the copied fixed body.
    assert "hls_thread_local hls::task t0(mem_r_stream_task<64>, s_cmd, m_mem, m_out);" in r
    assert "hls_thread_local hls::task t0(mem_w_stream_task<64>, s_cmd, s_in, m_mem);" in w
    # @port_read capability -> const pointer for the read owner; plain for the write owner.
    assert "const ap_uint<64>* m_mem" in r
    assert "const ap_uint<64>* m_mem" not in w

    # the fixed, width-templated task body is copied (not inlined into the top).
    rtask = (tmp_path / "include" / "mem_r_stream_task.h").read_text()
    wtask = (tmp_path / "include" / "mem_w_stream_task.h").read_text()
    assert "template <int MEM_DW>" in rtask and "template <int MEM_DW>" in wtask
    assert "c.read_stream<MEM_DW>(s_cmd);" in rtask
    assert "m_out.write(m_mem[w0 + w]);" in rtask
    assert "m_mem[w0 + w] = s_in.read();" in wtask
    assert "while" not in rtask and "while" not in wtask   # single-firing bodies
    # the command struct header is generated (single source with the pysim .get()).
    assert (tmp_path / "include" / "m_r_cmd.h").exists()
    assert (tmp_path / "include" / "m_w_cmd.h").exists()
