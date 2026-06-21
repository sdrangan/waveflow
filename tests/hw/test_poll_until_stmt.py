"""poll_until lowering — IR extraction + C++ emission (step 5).

``MMIFMaster.poll_until`` is ``@synthesizable``: a call extracts to a
:class:`PollUntilStmt` carrying the watched address, the lowered ``PollCond``
(op + const-or-HwVar rhs), and the (sim-only) poll_interval, and lowers to a call
into the reusable ring-poll primitive ``poll_until_impl::poll_until_{eq,ne}``.
Mirrors ``tests/hw/test_aximm_queue_stmt.py``.
"""
from __future__ import annotations

from waveflow.build.hwcodegen import HwStmtExtractor, SynthesisError
from waveflow.build.hwgen import CodegenCtx, to_cpp
from waveflow.hw.aximm_queue import AXIMMQueue, AXIMMQueueLayout
from waveflow.hw.dataschema import IntField
from waveflow.hw.hw_component import HwComponent, HwParam
from waveflow.hw.hwstmt import HwVar
from waveflow.hw.memif import Eq, LoweredPollCond, MMIFMaster, Ne, PollUntilStmt
from waveflow.simulation.simulation import Simulation

import pytest

_DemoCmd = IntField.specialize(bitwidth=32, signed=False)


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

class _PollConstConsumer(HwComponent):
    """Polls a flag word until it equals a constant (rhs is a literal)."""

    poll_addr: HwParam[int] = 0x40
    poll_ticks: HwParam[int] = 8

    def __post_init__(self) -> None:
        super().__post_init__()
        self.m_mem = MMIFMaster(name=f"{self.name}_m_mem", sim=self.sim, bitwidth=64)
        self.add_endpoint(self.m_mem)

    def run_proc(self):
        while True:
            v = yield from self.m_mem.poll_until(self.poll_addr, Eq(1), self.poll_ticks)
            return v


class _PollNeConsumer(HwComponent):
    """Reads ``head`` then polls ``tail != head`` (rhs is a runtime HwVar)."""

    def __post_init__(self) -> None:
        super().__post_init__()
        self.m_mem = MMIFMaster(name=f"{self.name}_m_mem", sim=self.sim, bitwidth=64)
        self.add_endpoint(self.m_mem)
        layout = AXIMMQueueLayout(
            base_addr=0, capacity=8, elem_words=_DemoCmd.nwords_per_inst(64), mem_bw=64,
        )
        self.cmd_queue = AXIMMQueue(master=self.m_mem, layout=layout)

    @property
    def Cmd(self) -> type:
        return _DemoCmd

    def run_proc(self):
        while True:
            head = yield from self.cmd_queue.get(self.Cmd)
            v = yield from self.m_mem.poll_until(0x40, Ne(head), 8)
            return v


def test_poll_until_extracts_to_stmt_with_const_cond():
    tree = HwStmtExtractor(_PollConstConsumer(name="c", sim=Simulation())).extract()
    stmt = tree.body.stmts[0]
    assert isinstance(stmt, PollUntilStmt)
    assert stmt.outputs[0].name == "v"
    cond = stmt.cond
    assert isinstance(cond, LoweredPollCond)
    assert cond.op == "==" and cond.rhs == 1
    # poll_interval is captured as an input but is sim-only (not emitted).
    assert len(stmt.inputs) == 3


def test_poll_until_extracts_runtime_var_rhs():
    extractor = HwStmtExtractor(_PollNeConsumer(name="c", sim=Simulation()))
    tree = extractor.extract()
    get_stmt, poll_stmt = tree.body.stmts[0], tree.body.stmts[1]
    assert isinstance(poll_stmt, PollUntilStmt)
    cond = poll_stmt.cond
    assert cond.op == "!="
    # the rhs is the SAME HwVar bound by the prior queue get — a runtime read.
    assert isinstance(cond.rhs, HwVar)
    assert cond.rhs is get_stmt.outputs[0]


def test_poll_until_rejects_multi_arg_cond():
    class _Bad(HwComponent):
        def __post_init__(self) -> None:
            super().__post_init__()
            self.m_mem = MMIFMaster(name=f"{self.name}_m", sim=self.sim, bitwidth=64)
            self.add_endpoint(self.m_mem)

        def run_proc(self):
            while True:
                v = yield from self.m_mem.poll_until(0x40, Eq(1, 2), 8)  # noqa
                return v

    with pytest.raises(SynthesisError, match="exactly one positional"):
        HwStmtExtractor(_Bad(name="c", sim=Simulation())).extract()


# ---------------------------------------------------------------------------
# Emission
# ---------------------------------------------------------------------------

class _FakeMaster:
    bitwidth = 64


class _FakeBound:
    def __init__(self, master) -> None:
        self.__self__ = master


def _poll_stmt(cond, *, out="tail", addr=0x40):
    master = _FakeMaster()
    comp = HwComponent(name="c", sim=Simulation())
    comp.gmem = master  # discoverable by _endpoint_name
    stmt = PollUntilStmt(
        method=_FakeBound(master),
        inputs=[addr, cond, 8],
        outputs=[HwVar(name=out, typ=None)],
    )
    return stmt, comp


def test_poll_until_emits_ne_primitive_with_var_rhs():
    stmt, comp = _poll_stmt(LoweredPollCond("!=", HwVar(name="head", typ=_DemoCmd)))
    out = to_cpp(stmt, CodegenCtx(comp=comp))
    assert out == (
        "    ap_uint<64> tail = poll_until_impl::poll_until_ne<64>("
        "gmem, memmgr::byte_addr_to_word_index<64>(64), (ap_uint<64>)head);"
    )


def test_poll_until_emits_eq_primitive_with_const_rhs():
    stmt, comp = _poll_stmt(LoweredPollCond("==", 1), out="flag")
    out = to_cpp(stmt, CodegenCtx(comp=comp))
    assert out == (
        "    ap_uint<64> flag = poll_until_impl::poll_until_eq<64>("
        "gmem, memmgr::byte_addr_to_word_index<64>(64), (ap_uint<64>)1);"
    )


def test_poll_interval_is_not_emitted():
    # The AT-model poll_interval (inputs[2]) must not appear in the lowered C++.
    stmt, comp = _poll_stmt(LoweredPollCond("==", 1))
    out = to_cpp(stmt, CodegenCtx(comp=comp))
    assert "8" not in out.split("poll_until_eq")[1]  # 8 = poll_interval, never emitted


# ---------------------------------------------------------------------------
# Full extract -> resolve -> codegen (header + body), no Vitis
# ---------------------------------------------------------------------------

class _PollKernel(HwComponent):
    """A minimal synthesizable kernel that polls a flag word, for end-to-end codegen."""

    cpp_kernel_name = "poll_kernel"
    cpp_namespace = ""

    def __post_init__(self) -> None:
        super().__post_init__()
        self.m_mem = MMIFMaster(name=f"{self.name}_m_mem", sim=self.sim, bitwidth=64)
        self.add_endpoint(self.m_mem)

    def run_proc(self):
        while True:
            flag = yield from self.m_mem.poll_until(0x40, Eq(1), 8)  # noqa: F841
            return


def test_kernel_body_emits_poll_primitive_call():
    from waveflow.build.hwgen import kernel_body_to_cpp
    body = kernel_body_to_cpp(_PollKernel(name="pk", sim=Simulation()))
    assert (
        "ap_uint<64> flag = poll_until_impl::poll_until_eq<64>("
        "m_mem, memmgr::byte_addr_to_word_index<64>(64), (ap_uint<64>)1);"
    ) in body


def test_header_includes_poll_primitive_and_memmgr():
    from waveflow.build.hwgen import header_to_cpp
    hdr = header_to_cpp(_PollKernel)
    assert '#include "poll_until_impl.tpp"' in hdr
    assert '#include "include/memmgr.hpp"' in hdr
