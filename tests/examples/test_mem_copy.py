"""Tests for the MemCopy composition de-risk (Phase 2, Gate 2).

Rung-0 (pysim) leg: the composite golden is bit-exact — ``Sequencer -> MemRStream -> MemWStream``
memcpy's a word run from one region to another over internal FIFOs, and emits one ``s_done`` token per
job.  Plus the **graph-derived** composite codegen shape (the real Phase-2 deliverable): one
``ap_ctrl_none`` top instantiating three ``hls::task`` bodies wired by ``hls_thread_local`` streams,
with two ``m_axi`` bundles + two AXIS ports on the boundary.  The csynth + XSI legs of Gate 2 need
Vitis/Vivado and are driven out-of-band by ``examples/mem_copy/mem_copy.py`` +
``examples/interleaver/xsi/run.bat``.
"""
from __future__ import annotations

from pathlib import Path


def test_copycmd_schema_single_source():
    """CopyCmd packs {src_off, dst_off, n_words} — two 64-bit words at MEM_DW=64 (LSB-first)."""
    from examples.mem_copy.mem_copy import CopyCmd

    assert CopyCmd.nwords_per_inst(64) == 2
    c = CopyCmd(src_off=16, dst_off=600, n_words=128)
    w = c.serialize(word_bw=64)
    assert int(w[0]) == 16 | (600 << 32)           # src_off low, dst_off high (word 0)
    assert int(w[1]) == 128                         # n_words (word 1)
    d = CopyCmd().deserialize(w, word_bw=64)
    assert int(d.src_off) == 16 and int(d.dst_off) == 600 and int(d.n_words) == 128


def test_mem_copy_pysim_golden():
    """MemCopy memcpy's a region bit-exact (run_copy asserts internally on mismatch)."""
    from examples.mem_copy.mem_copy_sim import run_copy
    for n in (128, 1, 257):                        # 1 = single word; 257 = multiple AXI bursts
        c = run_copy(jobs=((16, 600, n),))
        assert c.wstream.transfer_spans           # ran, recorded a span


def test_mem_copy_back_to_back():
    """Two CopyCmds to distinct offsets exercise the free-running hls::task re-fire across jobs;
    both copies are bit-exact and exactly two done tokens are emitted."""
    from examples.mem_copy.mem_copy_sim import run_copy
    run_copy(jobs=((16, 600, 128), (200, 900, 64)))


def test_mem_copy_codegen_shape(tmp_path: Path):
    """The generated composite top is a free-running ap_ctrl_none top that instantiates the three
    sub-component task bodies and wires them with hls_thread_local internal streams derived from the
    component/interface graph — no #define MEM_DW, no while."""
    from examples.mem_copy.mem_copy import generate

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


def test_xsi_vectors_header_is_current():
    """The committed mem_copy_vectors.h must equal what the schema produces NOW.

    This is the check that makes Stage 4 stick, and it deliberately needs no toolchain so it runs in
    the fast loop. The XSI TB cannot call CopyCmd::write_stream — it is host-compiled and cannot
    include copy_cmd.h (ap_int/hls_stream) — so its command words are the *output* of
    CopyCmd.serialize(), baked into a generated header. That removes the second implementation, but
    it leaves the header able to go stale: change CopyCmd's layout and the TB would keep driving the
    old words, testing the wrong thing while passing.

    If this fails: re-run `python examples/mem_copy/mem_copy.py`.
    """
    from pathlib import Path as _P
    from examples.mem_copy.mem_copy import render_xsi_vectors

    committed = (_P(__file__).resolve().parents[2] / "examples" / "mem_copy" / "xsi"
                 / "mem_copy_vectors.h").read_text(encoding="utf-8").replace("\r\n", "\n")
    assert render_xsi_vectors(64) == committed, (
        "xsi/mem_copy_vectors.h is stale — the schema or scenario changed under it. "
        "Regenerate: python examples/mem_copy/mem_copy.py"
    )


def test_xsi_vectors_are_the_schemas_own_serialization():
    """The generated words ARE CopyCmd.serialize()'s output, not a re-derivation of it.

    Pins the property the header exists for. If someone 'optimises' the generator into a hand-rolled
    packer, the second implementation is back and this fails.
    """
    import re

    from examples.mem_copy.mem_copy import (
        XSI_DST_W, XSI_N, XSI_SRC_W, CopyCmd, render_xsi_vectors,
    )
    from waveflow.hw.mem_stream import MemComplete

    h = render_xsi_vectors(64)
    emitted = [int(x) for x in re.search(r"CMD_WORDS\[\d+\] = \{ ([^}]*) \}", h).group(1)
               .replace("ULL", "").split(",")]

    expect: list[int] = []
    for s, d in zip(XSI_SRC_W, XSI_DST_W):
        expect.extend(int(w) for w in CopyCmd(src_off=s, dst_off=d, n_words=XSI_N)
                      .serialize(word_bw=64))
    assert emitted == expect

    # s_done framing is a schema fact, not a testbench constant.
    assert f"DONE_WORDS = {MemComplete.nwords_per_inst(64)};" in h


def test_sequencer_run_iter_is_extractable():
    """Sequencer is a FreeRunComp whose run_iter lowers as a leaf: ``get`` -> hook -> ``write``.

    This IS what MemCopy builds with: ``TaskBodyStep`` generates ``include/mem_seq_task.h`` from
    ``run_iter``, and the composite top instantiates it as ``mem_seq_task<64>``.  The test pins the
    shape that makes that possible, so it cannot rot silently: the per-job counter stays behind the
    ``@synthesizable`` ``next_xfer_msg`` boundary (a lowered body may not read mutable ``self.X``),
    and the commands are built in hooks (constructing a DataSchema is not in the extractor's
    vocabulary).  Inlining either back into ``run_iter`` fails here — and would break the build.
    """
    from waveflow.build.codegen_dispatch import codegen_path
    from waveflow.build.hwcodegen import extract_kernel
    from waveflow.build.hwgen import kernel_files_to_str
    from waveflow.simulation.simulation import Simulation
    from examples.mem_copy.mem_copy import Sequencer

    seq = Sequencer(name="seq", sim=Simulation(), mem_dwidth=64)
    path = codegen_path(seq)
    assert (path.kind, path.method) == ("leaf", "run_iter")
    extract_kernel(seq)                                  # raises SynthesisError if the shape breaks

    files = kernel_files_to_str(Sequencer)
    body = files["mem_seq.cpp"]
    # the three hooks are declarations; their bodies are hand-written stubs, not lowered Python.
    for hook in ("next_xfer_msg", "make_mr_cmd", "make_mw_cmd"):
        assert f"mem_seq_impl::{hook}" in body, f"{hook} not called from the generated body"
        assert f"mem_seq_{hook}_impl.cpp" in files, f"{hook} stub not emitted"
    assert "job_idx" not in body, "the counter must stay in the hook, not the lowered body"
    # one firing per command: the hls::task runtime re-fires, so no loop belongs here.
    assert "while" not in body and "for (" not in body


def test_sequencer_codegen_gaps_are_still_open():
    """TRIPWIRE: the gaps in the STANDALONE-top product (kernel_files_to_str), not the task body.

    These two gaps no longer block MemCopy — the composite builds a *generated* task body via
    ``TaskBodyStep``, and a body needs neither an ``ap_ctrl_none`` pragma (bodies carry no pragmas)
    nor a boundary stream type.  What they still block is ``free_running_kernel``: Sequencer compiled
    as its OWN ``ap_ctrl_none`` top, which is a different declared target.

    If this test FAILS, that is probably good news — someone implemented ``free_running_kernel`` or
    aligned the stream convention.  Update the docs and delete this test.
    """
    from waveflow.build.hwgen import kernel_files_to_str
    from waveflow.hw.codegen_targets import FREE_RUNNING_KERNEL, IMPLEMENTED_TARGETS
    from examples.mem_copy.mem_copy import Sequencer

    assert FREE_RUNNING_KERNEL not in IMPLEMENTED_TARGETS

    body = kernel_files_to_str(Sequencer)["mem_seq.cpp"]
    # Gap 1: emits a control-driven top, not the ap_ctrl_none a free-running hls::task needs.
    assert "s_axilite port=return" in body and "ap_ctrl_none" not in body
    # Gap 2: emits axi4s_word streams; the composite's tasks take hls::stream<ap_uint<W>> + read_stream<W>.
    assert "axi4s_word" in body and "ap_uint" not in body


def test_composite_top_spec_is_graph_derived():
    """The composite TopSpec is derived from the sub_comps + internal interfaces (add_comp/add_if),
    not hand-written: each task arg resolves an endpoint to a FIFO or boundary port."""
    from waveflow.simulation.simulation import Simulation
    from examples.mem_copy.mem_copy import MemCopy, composite_top_spec

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
