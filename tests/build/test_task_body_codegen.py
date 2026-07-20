"""Tests for `task_files_to_str` — the composite **task body** product (composite_kernel's leaf half).

A task body is not a top, and every assertion here is really about that one distinction: it carries
no interface pragmas (the composite top owns the interface), it is templated + `static` (the top
instantiates `hook_seq_task<64>` and `#include`s the header into one TU), and it speaks the
word-granular stream convention (`hls::stream<ap_uint<W>>` + `read_stream<W>`) that the composite's
`hls_thread_local` FIFOs are declared with. Contrast `kernel_files_to_str`, which emits a standalone
top that cannot be wired into a composite.

The subject is a **local fixture** (:class:`_HookSeq`), not any shipped example: its hooks construct a
``DataSchema`` (not in the extractor's vocabulary), so they lower to hook CALLS + TODO stubs — the
delegation path this file pins.  mem_copy's Sequencer used to be that fixture, until it was retired to
a hand-written framed body (plans/memcopy_inband_integration.md Stage 4).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar

import pytest

from waveflow.build.hwgen import kernel_files_to_str, task_files_to_str
from waveflow.hw.clock import Clock
from waveflow.hw.dataschema import DataList, IntField
from waveflow.hw.hw_component import HwParam
from waveflow.hw.hw_freerun import FreeRunComp
from waveflow.hw.interface import StreamIFMaster, StreamIFSlave
from waveflow.hw.synth import synthesizable
from waveflow.simulation.simobj import ProcessGen
from waveflow.simulation.simulation import Simulation

_W32 = IntField.specialize(bitwidth=32, signed=False)


class _Cmd(DataList):
    """Two packed words — the fixture's stream payload."""
    elements = {
        "a": {"schema": _W32, "description": "first field"},
        "b": {"schema": _W32, "description": "second field"},
    }


@dataclass
class _HookSeq(FreeRunComp):
    """Minimal extractable task-body fixture: ``get`` -> hook -> ``write`` -> hook -> ``write``.

    Each hook constructs a :class:`_Cmd`, which is **not** in the extractor's vocabulary (constructing
    a ``DataSchema`` is rejected), so it lowers to a hook CALL + a hand-written TODO stub rather than
    inlined arithmetic — exactly the delegation shape this file exists to pin."""

    cpp_kernel_name: ClassVar[str | None] = "hook_seq"
    cpp_namespace: ClassVar[str | None] = "hook_seq_impl"

    mem_dwidth: HwParam[int] = 64
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        self.s_in = StreamIFSlave(name=f"{self.name}_s_in", sim=self.sim, bitwidth=self.mem_dwidth)
        self.m_out = StreamIFMaster(name=f"{self.name}_m_out", sim=self.sim, bitwidth=self.mem_dwidth)
        self.add_endpoint(self.s_in)
        self.add_endpoint(self.m_out)

    @synthesizable
    def make_a(self, cmd: _Cmd) -> _Cmd:
        return _Cmd(a=int(cmd.a), b=0)

    @synthesizable
    def make_b(self, cmd: _Cmd) -> _Cmd:
        return _Cmd(a=0, b=int(cmd.b))

    def run_iter(self) -> ProcessGen[None]:
        cmd: _Cmd = yield from self.s_in.get(_Cmd)
        a = self.make_a(cmd)
        yield from self.m_out.write(a)
        b = self.make_b(cmd)
        yield from self.m_out.write(b)


def _task_h() -> str:
    return task_files_to_str(_HookSeq)["hook_seq_task.h"]


def test_task_body_is_templated_static_and_pragma_free():
    """The three ways a task body differs from a top."""
    h = _task_h()

    # Templated on the HwParam-driven width, NOT the baked default. task_files_to_str takes a
    # *class* and reads the default variant, so a concrete body would emit ap_uint<64> even for a
    # 32-wide instance -- the template is what keeps it honest.
    assert "template <int MEM_DWIDTH>" in h
    assert "static void hook_seq_task(" in h
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
    files = task_files_to_str(_HookSeq)
    h = files["hook_seq_task.h"]

    for hook in ("make_a", "make_b"):
        assert f"hook_seq_impl::{hook}(" in h, f"{hook} not called from the generated body"
        assert f"hook_seq_{hook}_impl.cpp" in files, f"{hook} stub not emitted"
    assert "namespace hook_seq_impl {" in h

    # The stub is scaffolding, not an implementation -- it must be obviously unfinished.
    stub = files["hook_seq_make_a_impl.cpp"]
    assert "TODO: implement make_a" in stub
    assert '#include "hook_seq_task.h"' in stub, "stub must include the task header (it has no .hpp)"


def test_task_body_ordering_matches_run_iter():
    """Reads/writes appear in the order run_iter states them -- this is what is actually derived."""
    h = _task_h()
    # Search the function body only: the hook *declarations* appear earlier, in the namespace block,
    # so indexing the whole header would compare a decl against a call site.
    body = h[h.index("static void hook_seq_task("):]
    order = [
        body.index("cmd.read_stream"),
        body.index("make_a"),
        body.index("a.write_stream"),
        body.index("make_b"),
        body.index("b.write_stream"),
    ]
    assert order == sorted(order), f"emitted body is out of run_iter order: {order}"


def test_generated_cpp_is_ascii():
    """Generated C++ must be ASCII: it is written to disk on a cp1252 host and fed to Vitis."""
    for fn, content in task_files_to_str(_HookSeq).items():
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
    top = kernel_files_to_str(_HookSeq)["hook_seq.cpp"]
    body = _task_h()

    # The top has an interface and AXI4-Stream words; it cannot bind to an ap_uint FIFO.
    assert "#pragma HLS INTERFACE" in top and "axi4s_word" in top
    # The body has neither, and is templated where the top is concrete.
    assert "#pragma HLS INTERFACE" not in body and "axi4s_word" not in body
    assert "template <int" in body and "template <int" not in top


def test_fixture_extracts_and_simulates():
    """Sanity: the fixture really is extractable (codegen path is a leaf run_iter) so the assertions
    above are exercising the generated-body path, not a degenerate one."""
    from waveflow.build.codegen_dispatch import codegen_path

    seq = _HookSeq(name="hs", sim=Simulation(), mem_dwidth=64)
    path = codegen_path(seq)
    assert (path.kind, path.method) == ("leaf", "run_iter")
