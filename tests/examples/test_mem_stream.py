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
    """MRCmd / MWCmd pack {byte_addr, n_words} into one 64-bit word (LSB-first)."""
    from examples.interleaver.mem_stream import MRCmd, MWCmd

    assert MRCmd.nwords_per_inst(64) == 1
    assert MRCmd.nwords_per_inst(32) == 2
    c = MRCmd(byte_addr=800, n_words=128)
    w = c.serialize(word_bw=64)
    assert int(w[0]) == 800 | (128 << 32)          # byte_addr low, n_words high
    d = MRCmd().deserialize(w, word_bw=64)
    assert int(d.byte_addr) == 800 and int(d.n_words) == 128
    # mirror schema
    assert MWCmd.nwords_per_inst(64) == 1


def test_mem_r_stream_pysim_golden():
    """MemRStream bursts a known memory region onto m_out bit-exact."""
    from examples.interleaver.mem_stream_sim import run_read
    assert run_read(n_words=128) is True
    assert run_read(n_words=1) is True            # single-word edge
    assert run_read(n_words=257) is True          # spans multiple AXI bursts


def test_mem_w_stream_pysim_golden():
    """MemWStream drains a known stream into a memory region bit-exact."""
    from examples.interleaver.mem_stream_sim import run_write
    assert run_write(n_words=128) is True
    assert run_write(n_words=1) is True
    assert run_write(n_words=257) is True


def test_mem_stream_codegen_shape(tmp_path: Path):
    """Template codegen emits a free-running ap_ctrl_none single-hls::task kernel per endpoint,
    with the read owner's m_mem a const pointer and the write owner's a plain pointer."""
    from examples.interleaver.mem_stream_gen import generate

    generate(out_dir=tmp_path)
    r = (tmp_path / "gen" / "mem_r_stream.cpp").read_text()
    w = (tmp_path / "gen" / "mem_w_stream.cpp").read_text()
    for src in (r, w):
        assert "#pragma HLS INTERFACE ap_ctrl_none port=return" in src
        assert "hls_thread_local hls::task" in src
        assert 'c.read_stream<MEM_DW>(s_cmd);' in src
    # @port_read capability -> const pointer for the read owner; plain for the write owner.
    assert "const ap_uint<MEM_DW>* m_mem" in r
    assert "const ap_uint<MEM_DW>* m_mem" not in w
    assert "m_out.write(m_mem[w0 + w]);" in r
    assert "m_mem[w0 + w] = s_in.read();" in w
    # the command struct header is generated (single source with the pysim .get()).
    assert (tmp_path / "include" / "m_r_cmd.h").exists()
    assert (tmp_path / "include" / "m_w_cmd.h").exists()
