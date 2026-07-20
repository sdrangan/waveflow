"""Tests for the MemCopy composition de-risk (Phase 2, Gate 2).

Rung-0 (pysim) leg: the composite golden is bit-exact — ``Sequencer -> MemRStream -> MemWStream``
memcpy's a word run from one region to another over TWO in-band framed FIFOs, and emits a
``CopyResp`` completion per job.  Plus the **graph-derived** composite codegen shape (the
real Phase-2 deliverable): one ``ap_ctrl_none`` top instantiating three ``hls::task`` bodies wired by
``hls_thread_local`` ``framed_word`` streams derived from the component/interface graph, with two
``m_axi`` bundles + two AXIS ports on the boundary.  The csynth + XSI legs of Gate 2 need Vitis/Vivado
and are driven out-of-band by ``examples/mem_copy/mem_copy.py`` + ``examples/mem_copy/xsi/run.bat`` —
the in-band gate is 2910 (plans/memcopy_inband_integration.md).
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
    """MemCopy memcpy's a region bit-exact over the framed chain (run_copy asserts on mismatch)."""
    from examples.mem_copy.mem_copy_sim import run_copy
    for n in (128, 1, 257):                        # 1 = single word; 257 = multiple AXI bursts
        c = run_copy(jobs=((16, 600, n),))
        assert c.wstream.transfer_spans           # ran, recorded a span


def test_mem_copy_framed_desync_proof(tmp_path):
    """The framed chain copies bit-exact across mixed-size jobs and echoes the right tx_id per job:
    Sequencer frames [MemRCmd|MemWCmd|CopyResp], the reader relays the opaque prefix + fetches src, the
    writer decodes MemWCmd and writes dst.  Command/data ride one framed stream, so they cannot desync.
    See plans/memcopy_inband_integration.md."""
    import numpy as np
    from examples.mem_copy.mem_copy import CopyJob
    from examples.mem_copy.mem_copy_sim import MemCopySim

    jobs = (CopyJob(16, 512, 128), CopyJob(200, 900, 64), CopyJob(400, 1300, 16))
    sim = MemCopySim(jobs=jobs, mem_dwidth=64)
    sim.write_scenario(tmp_path)
    sim.tb.sim.run_sim()
    for job, exp in zip(sim.tb._jobs, sim.expected):
        got = sim.tb.mem._mem.read(job.dst_off * 8, job.n_words).astype(np.uint64)
        assert np.array_equal(got, exp), f"framed copy wrong at dst={job.dst_off}"
    # one CopyResp burst per job
    assert len(sim.tb.done_sink.words) == len(jobs)


def test_mem_copy_back_to_back():
    """Two CopyCmds to distinct offsets exercise the free-running hls::task re-fire across jobs;
    both copies are bit-exact and exactly two done burst-pairs are emitted."""
    from examples.mem_copy.mem_copy_sim import run_copy
    run_copy(jobs=((16, 600, 128), (200, 900, 64)))


def test_mem_copy_codegen_shape(tmp_path: Path):
    """The generated composite top is a free-running ap_ctrl_none top that instantiates the three
    framed sub-component task bodies and wires them with hls_thread_local ``framed_word`` internal
    streams derived from the component/interface graph — no #define MEM_DW, no while."""
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

    # two internal FRAMED hls_thread_local FIFOs (framed_word carries the packet boundary; the two
    # boundary ports stay word-flavor).  Graph-derived edge names (StreamIF name minus prefix/_if).
    for fifo in ("cmd", "copy_data"):
        assert f"hls_thread_local hls::stream<streamutils::framed_word<64> > {fifo};" in src

    # three tasks at a concrete width, wired exactly as the graph specifies (the writer's payload
    # buffer bound max_xfer_len=8 is a second template arg).
    assert "hls_thread_local hls::task t0(mem_seq_framed_task<64>, s_cmd, cmd);" in src
    assert "hls_thread_local hls::task t1(mem_r_stream_framed_task<64>, cmd, m_in, copy_data);" in src
    assert ("hls_thread_local hls::task t2(mem_w_stream_framed_done_task<64, 8>, "
            "copy_data, m_out, s_done);") in src

    # the copied fixed body headers + framed command struct headers exist in include/.
    for h in ("mem_seq_framed_task.h", "mem_r_stream_framed_task.h", "mem_w_stream_framed_done_task.h",
              "copy_cmd.h", "mem_r_cmd.h", "mem_w_cmd.h", "copy_resp.h"):
        assert (tmp_path / "include" / h).exists(), h


def test_xsi_vectors_header_is_current():
    """The committed mem_copy_vectors.h must equal what the schema produces NOW.

    This is the check that makes the retire stick, and it deliberately needs no toolchain so it runs in
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
        XSI_DST_W, XSI_N, XSI_SRC_W, CopyCmd, _done_words, render_xsi_vectors,
        write_mem_copy_xsi_bundles,
    )
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
    # s_done framing is a schema fact, still introspected into the header (one CopyResp == 1 word).
    assert f"DONE_WORDS = {_done_words(64)};" in h


def test_composite_top_spec_is_graph_derived():
    """The composite TopSpec is derived from the sub_comps + internal interfaces (add_comp/add_if),
    not hand-written: each task arg resolves an endpoint to a framed FIFO or boundary port."""
    from waveflow.simulation.simulation import Simulation
    from examples.mem_copy.mem_copy import MemCopy, composite_top_spec

    comp = MemCopy(name="mc", sim=Simulation(), mem_dwidth=64)
    # graph is registered on the parent
    assert set(comp.sub_comps) == {"mc_seq", "mc_r", "mc_w"}
    assert len(comp.interfaces) == 2               # cmd + copy_data framed edges

    spec = composite_top_spec(comp, width=64)
    assert len(spec.tasks) == 3
    assert len(spec.internal_streams) == 2
    # the read task's m_mem resolves to the gmem0 boundary; its s_cmd/m_out to internal framed FIFOs.
    r_task = next(t for t in spec.tasks if t.task_fn == "mem_r_stream_framed_task")
    assert r_task.args == ("cmd", "m_in", "copy_data")
