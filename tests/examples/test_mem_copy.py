"""Tests for the MemCopy composition de-risk (Phase 2, Gate 2).

Rung-0 (pysim) leg: the composite golden is bit-exact — ``Sequencer -> MemRStream -> MemWStream``
memcpy's a word run from one region to another over internal FIFOs, and emits one ``s_done`` token per
job.  Plus the **graph-derived** composite codegen shape (the real Phase-2 deliverable): one
``ap_ctrl_none`` top instantiating three ``hls::task`` bodies wired by ``hls_thread_local`` streams,
with two ``m_axi`` bundles + two AXIS ports on the boundary.  The csynth + XSI legs of Gate 2 need
Vitis/Vivado and are driven out-of-band by ``examples/interleaver/mem_copy.py`` +
``examples/interleaver/xsi/run.bat``.
"""
from __future__ import annotations

from pathlib import Path


def test_copycmd_schema_single_source():
    """CopyCmd packs {src_off, dst_off, n_words} — two 64-bit words at MEM_DW=64 (LSB-first)."""
    from examples.interleaver.mem_copy import CopyCmd

    assert CopyCmd.nwords_per_inst(64) == 2
    c = CopyCmd(src_off=16, dst_off=600, n_words=128)
    w = c.serialize(word_bw=64)
    assert int(w[0]) == 16 | (600 << 32)           # src_off low, dst_off high (word 0)
    assert int(w[1]) == 128                         # n_words (word 1)
    d = CopyCmd().deserialize(w, word_bw=64)
    assert int(d.src_off) == 16 and int(d.dst_off) == 600 and int(d.n_words) == 128


def test_mem_copy_pysim_golden():
    """MemCopy memcpy's a region bit-exact (run_copy asserts internally on mismatch)."""
    from examples.interleaver.mem_copy_sim import run_copy
    for n in (128, 1, 257):                        # 1 = single word; 257 = multiple AXI bursts
        c = run_copy(jobs=((16, 600, n),))
        assert c.wstream.transfer_spans           # ran, recorded a span


def test_mem_copy_back_to_back():
    """Two CopyCmds to distinct offsets exercise the free-running hls::task re-fire across jobs;
    both copies are bit-exact and exactly two done tokens are emitted."""
    from examples.interleaver.mem_copy_sim import run_copy
    run_copy(jobs=((16, 600, 128), (200, 900, 64)))


def test_mem_copy_codegen_shape(tmp_path: Path):
    """The generated composite top is a free-running ap_ctrl_none top that instantiates the three
    sub-component task bodies and wires them with hls_thread_local internal streams derived from the
    component/interface graph — no #define MEM_DW, no while."""
    from examples.interleaver.mem_copy import generate

    generate(out_dir=tmp_path, width=64)
    src = (tmp_path / "gen" / "mem_copy.cpp").read_text()

    assert "#pragma HLS INTERFACE ap_ctrl_none port=return" in src
    assert "#define MEM_DW" not in src              # concrete width baked in, not a macro
    assert "while" not in src                        # single-firing; the hls::task runtime re-fires

    # two m_axi bundles (read const on gmem0, write plain on gmem1) + two AXIS boundary ports.
    assert "#pragma HLS INTERFACE m_axi port=m_in offset=slave bundle=gmem0" in src
    assert "#pragma HLS INTERFACE m_axi port=m_out offset=slave bundle=gmem1" in src
    assert "const ap_uint<64>* m_in" in src         # @port_read -> const pointer
    assert "ap_uint<64>* m_out" in src and "const ap_uint<64>* m_out" not in src
    assert "#pragma HLS INTERFACE axis port=s_cmd" in src
    assert "#pragma HLS INTERFACE axis port=s_done" in src

    # three internal hls_thread_local FIFOs wiring the tasks (graph-derived edge names).
    for fifo in ("mr_cmd", "mw_cmd", "copy_data"):
        assert f"hls_thread_local hls::stream<ap_uint<64> > {fifo};" in src

    # three tasks at a concrete width, wired exactly as the graph specifies.
    assert "hls_thread_local hls::task t0(mem_seq_task<64>, s_cmd, mr_cmd, mw_cmd);" in src
    assert "hls_thread_local hls::task t1(mem_r_stream_task<64>, mr_cmd, m_in, copy_data);" in src
    assert ("hls_thread_local hls::task t2(mem_w_stream_done_task<64>, "
            "mw_cmd, copy_data, m_out, s_done);") in src

    # the copied fixed body headers + command struct headers exist in include/.
    for h in ("mem_seq_task.h", "mem_r_stream_task.h", "mem_w_stream_done_task.h",
              "copy_cmd.h", "m_r_cmd.h", "m_w_cmd.h"):
        assert (tmp_path / "include" / h).exists(), h


def test_composite_top_spec_is_graph_derived():
    """The composite TopSpec is derived from the sub_comps + internal interfaces (add_comp/add_if),
    not hand-written: each task arg resolves an endpoint to a FIFO or boundary port."""
    from waveflow.simulation.simulation import Simulation
    from examples.interleaver.mem_copy import MemCopy, composite_top_spec

    comp = MemCopy(name="mc", sim=Simulation(), mem_dwidth=64)
    # graph is registered on the parent
    assert set(comp.sub_comps) == {"mc_seq", "mc_r", "mc_w"}
    assert len(comp.interfaces) == 3               # mr_cmd / mw_cmd / copy_data edges

    spec = composite_top_spec(comp, width=64)
    assert len(spec.tasks) == 3
    assert len(spec.internal_streams) == 3
    # the read task's m_mem resolves to the gmem0 boundary; its s_cmd/m_out to internal FIFOs.
    r_task = next(t for t in spec.tasks if t.task_fn == "mem_r_stream_task")
    assert r_task.args == ("mr_cmd", "m_in", "copy_data")
