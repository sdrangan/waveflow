"""Tests for `task_files_to_str` — the composite **task body** product (composite_kernel's leaf half).

A task body is not a top, and every assertion here is really about that one distinction: it carries
no interface pragmas (the composite top owns the interface), it is templated + `static` (the top
instantiates `mem_seq_task<64>` and `#include`s the header into one TU), and it speaks the
word-granular stream convention (`hls::stream<ap_uint<W>>` + `read_stream<W>`) that the composite's
`hls_thread_local` FIFOs are declared with. Contrast `kernel_files_to_str`, which emits a standalone
top that cannot be wired into a composite.
"""
from __future__ import annotations

import pytest

from waveflow.build.hwgen import kernel_files_to_str, task_files_to_str
from examples.mem_copy.mem_copy import Sequencer


def _task_h() -> str:
    return task_files_to_str(Sequencer)["mem_seq_task.h"]


def test_task_body_is_templated_static_and_pragma_free():
    """The three ways a task body differs from a top."""
    h = _task_h()

    # Templated on the HwParam-driven width, NOT the baked default. task_files_to_str takes a
    # *class* and reads the default variant, so a concrete body would emit ap_uint<64> even for a
    # 32-wide instance -- the template is what keeps it honest.
    assert "template <int MEM_DWIDTH>" in h
    assert "static void mem_seq_task(" in h
    assert "hls::stream<ap_uint<MEM_DWIDTH> >&" in h
    assert "ap_uint<64>" not in h, "width must be templated, not baked from the default variant"

    # No interface: the composite top owns it.
    assert "#pragma HLS INTERFACE" not in h
    assert "ap_ctrl" not in h and "s_axilite" not in h

    # Word-granular convention, not the top's AXI4-Stream word.
    assert "axi4s_word" not in h and "read_axi4_stream" not in h
    assert "read_stream<MEM_DWIDTH>" in h and "write_stream<MEM_DWIDTH>" in h

    # One firing per invocation: the hls::task runtime re-fires, so no loop belongs in the body.
    assert "while" not in h


def test_task_body_delegates_to_declared_hooks():
    """The body's structure comes from run_iter; the leaf computation stays hand-written."""
    files = task_files_to_str(Sequencer)
    h = files["mem_seq_task.h"]

    for hook in ("make_xfer_msg", "make_mr_cmd", "make_mw_cmd"):
        assert f"mem_seq_impl::{hook}(" in h, f"{hook} not called from the generated body"
        assert f"mem_seq_{hook}_impl.cpp" in files, f"{hook} stub not emitted"
    assert "namespace mem_seq_impl {" in h

    # The stub is scaffolding, not an implementation -- it must be obviously unfinished.
    stub = files["mem_seq_make_xfer_msg_impl.cpp"]
    assert "TODO: implement make_xfer_msg" in stub
    assert '#include "mem_seq_task.h"' in stub, "stub must include the task header (it has no .hpp)"

    # The cookie now comes from cmd.tx_id (no counter), so no cross-firing state leaks into the body.
    assert "job_idx" not in h


def test_task_body_ordering_matches_run_iter():
    """Reads/writes appear in the order run_iter states them -- this is what is actually derived."""
    h = _task_h()
    # Search the function body only: the hook *declarations* appear earlier, in the namespace block,
    # so indexing the whole header would compare a decl against a call site.
    body = h[h.index("static void mem_seq_task("):]
    order = [
        body.index("cmd.read_stream"),
        body.index("make_xfer_msg"),
        body.index("make_mr_cmd"),
        body.index("mr.write_stream"),
        body.index("make_mw_cmd"),
        body.index("mw.write_stream"),
    ]
    assert order == sorted(order), f"emitted body is out of run_iter order: {order}"


def test_generated_cpp_is_ascii():
    """Generated C++ must be ASCII: it is written to disk on a cp1252 host and fed to Vitis."""
    for fn, content in task_files_to_str(Sequencer).items():
        assert content.isascii(), f"{fn} contains non-ASCII characters"


def test_task_body_refuses_m_axi_components():
    """Stream-only today. An m_axi body raises questions this emitter has not answered (bundle
    naming, depth, who owns the offset register), so it refuses rather than emit something
    unreviewed -- mem_r/mem_w_stream_task.h stay hand-written."""
    from waveflow.hw.mem_stream import MemRStream

    with pytest.raises(NotImplementedError, match="stream-only"):
        task_files_to_str(MemRStream)


def test_task_body_and_standalone_top_are_different_products():
    """The same component lowers two ways; neither substitutes for the other."""
    top = kernel_files_to_str(Sequencer)["mem_seq.cpp"]
    body = _task_h()

    # The top has an interface and AXI4-Stream words; it cannot bind to an ap_uint FIFO.
    assert "#pragma HLS INTERFACE" in top and "axi4s_word" in top
    # The body has neither, and is templated where the top is concrete.
    assert "#pragma HLS INTERFACE" not in body and "axi4s_word" not in body
    assert "template <int" in body and "template <int" not in top
