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
    """CopyCmd packs {src_off, dst_off, n_words, tx_id} — two 64-bit words at MEM_DW=64 (LSB-first).

    Four Word32 fields still pack into two 64-bit words (128 bits), so adding tx_id did not widen the
    command; it fills word 1's previously-padding high half."""
    from examples.mem_copy.mem_copy import CopyCmd

    assert CopyCmd.nwords_per_inst(64) == 2
    c = CopyCmd(src_off=16, dst_off=600, n_words=128, tx_id=7)
    w = c.serialize(word_bw=64)
    assert int(w[0]) == 16 | (600 << 32)           # src_off low, dst_off high (word 0)
    assert int(w[1]) == 128 | (7 << 32)            # n_words low, tx_id high (word 1)
    d = CopyCmd().deserialize(w, word_bw=64)
    assert int(d.src_off) == 16 and int(d.dst_off) == 600 and int(d.n_words) == 128
    assert int(d.tx_id) == 7


def test_mem_copy_pysim_golden():
    """MemCopy memcpy's a region bit-exact (run_copy asserts internally on mismatch)."""
    from examples.mem_copy.mem_copy_sim import run_copy
    for n in (128, 1, 257):                        # 1 = single word; 257 = multiple AXI bursts
        c = run_copy(jobs=((16, 600, n),))
        assert c.wstream.transfer_spans           # ran, recorded a span


def test_mem_copy_inband_pysim_golden(tmp_path):
    """The in-band/framed MemCopy variant copies bit-exact: Sequencer frames [FwdCmd|WrCmd|payload],
    the reader relays the opaque prefix + fetches src, the writer decodes WrCmd and writes dst.

    The command/data cannot desync (one framed stream), and the default two-stream DUT is untouched.
    See plans/memcopy_inband_integration.md.
    """
    import numpy as np
    from waveflow.simulation.simulation import Simulation
    from examples.mem_copy.mem_copy import CopyJob
    from examples.mem_copy.mem_copy_sim import MemCopyTB

    jobs = (CopyJob(16, 512, 128), CopyJob(200, 900, 64), CopyJob(400, 1300, 16))
    sim = Simulation()
    tb = MemCopyTB(name="tb", sim=sim, mem_dwidth=64, jobs=jobs, inband=True)
    tb.write_scenario(tmp_path)
    sim.run_sim()
    for job, exp in zip(tb._jobs, tb.expected):
        got = tb.mem._mem.read(job.dst_off * 8, job.n_words).astype(np.uint64)
        assert np.array_equal(got, exp), f"inband copy wrong at dst={job.dst_off}"
    # one WrComplete + payload burst-pair per job
    assert len(tb.done_sink.words) == 2 * len(jobs)


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


def test_xsi_command_bundle_is_the_schemas_own_serialization(tmp_path):
    """The XSI command **bundle**'s words ARE CopyCmd.serialize()'s output, not a re-derivation.

    The harness loads ``vectors/s_cmd`` in pre_sim; its words are exactly what the schema packs, so no
    second packing implementation exists to drift.  ``CMD_WORDS`` is gone from the header — the command
    words live in the bundle now — and this pins the replacement.  Pure Python (no toolchain).
    """
    import numpy as np

    from examples.mem_copy.mem_copy import (
        XSI_DST_W, XSI_N, XSI_SRC_W, CopyCmd, render_xsi_vectors, write_mem_copy_xsi_bundles,
    )
    from waveflow.hw.mem_stream import MemComplete
    from waveflow.utils.burst_io import BOUNDS_NAME, read_burst_bundle

    write_mem_copy_xsi_bundles(tmp_path, width=64)
    got = read_burst_bundle(tmp_path / "vectors" / "s_cmd")
    emitted = [int(w) for w in np.concatenate(got)]

    expect: list[int] = []
    for j, (s, d) in enumerate(zip(XSI_SRC_W, XSI_DST_W)):
        expect.extend(int(w) for w in CopyCmd(src_off=s, dst_off=d, n_words=XSI_N, tx_id=j)
                      .serialize(word_bw=64))
    assert emitted == expect

    # bounds are cumulative end-indices; every CopyCmd packs to 2 words at MEM_DW=64.
    bounds = np.fromfile(tmp_path / "vectors" / "s_cmd" / BOUNDS_NAME, dtype="<u8")
    np.testing.assert_array_equal(bounds, 2 * np.arange(1, len(expect) // 2 + 1))

    h = render_xsi_vectors(64)
    assert "CMD_WORDS" not in h, "the command words moved to the bundle; the header must not bake them"
    # s_done framing is a schema fact, still introspected into the header.
    assert f"DONE_WORDS = {MemComplete.nwords_per_inst(64)};" in h


def test_sequencer_run_iter_is_extractable():
    """Sequencer is a FreeRunComp whose run_iter lowers as a leaf: ``get`` -> hook -> ``write``.

    This IS what MemCopy builds with: ``TaskBodyStep`` generates ``include/mem_seq_task.h`` from
    ``run_iter``, and the composite top instantiates it as ``mem_seq_task<64>``.  The test pins the
    shape that makes that possible, so it cannot rot silently: the correlation cookie and the two
    commands are built in ``@synthesizable`` hooks (constructing a DataSchema is not in the extractor's
    vocabulary), so ``run_iter`` is only ``get`` -> hook -> ``write``.  Inlining any of them back into
    ``run_iter`` fails here — and would break the build.
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
    for hook in ("make_xfer_msg", "make_mr_cmd", "make_mw_cmd"):
        assert f"mem_seq_impl::{hook}" in body, f"{hook} not called from the generated body"
        assert f"mem_seq_{hook}_impl.cpp" in files, f"{hook} stub not emitted"
    assert "job_idx" not in body, "the counter must stay in the hook, not the lowered body"
    # one firing per command: the hls::task runtime re-fires, so no loop belongs here.
    assert "while" not in body and "for (" not in body


def test_sequencer_codegen_gaps_are_still_open():
    """TRIPWIRE: the gaps in the OLD extractor's STANDALONE-top path (kernel_files_to_str).

    These two gaps no longer block MemCopy — the composite builds a *generated* task body via
    ``TaskBodyStep``, and a body needs neither an ``ap_ctrl_none`` pragma (bodies carry no pragmas)
    nor a boundary stream type.  What they still block is the OLD extractor emitting Sequencer as its
    own free-running top: `kernel_files_to_str` routes a `FreeRunComp` through the `control_driven`
    extractor, which emits `ap_ctrl_hs` + `axi4s_word`, not the `ap_ctrl_none` task the design needs.

    Note this is the *extractor* path, distinct from the graph path `check(..., composite_kernel)` now
    validates via `composite_top_spec` — that one IS implemented (Flow 2). This gap is the extractor's
    free-running emission, still unbuilt. If this test FAILS, someone aligned the extractor or the
    stream convention — update the docs and delete this test.
    """
    from waveflow.build.hwgen import kernel_files_to_str
    from examples.mem_copy.mem_copy import Sequencer

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
