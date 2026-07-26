"""Condition-IR: the ``==`` / ``!=`` rhs may be a runtime variable (a HwVar).

The synthesizable ``if`` (``CaseStmt``) historically lowered ``if var.field <op>
CONST``.  Step 5 of the poll_until model needs the rhs to also be a runtime-read
local — the ring-poll dequeue compares ``tail != head`` where ``head`` is a
prior read, not a literal.  This locks in:

* extraction binds a bare-name rhs in scope to its ``HwVar`` (and the constant
  path is unchanged),
* emission writes the variable name (not a literal) for a ``HwVar`` rhs.
"""
from __future__ import annotations

from waveflow.build.hwcodegen import HwStmtExtractor, SynthesisError
from waveflow.build.hwgen import CodegenCtx, to_cpp
from waveflow.hw.aximm_queue import AXIMMQueue, AXIMMQueueLayout
from waveflow.hw.dataschema import IntField
from waveflow.hw.hw_module import HwModule
from waveflow.hw.hwstmt import CaseStmt, HwVar, ReturnStmt, SeqStmt, WhileStmt
from waveflow.hw.memif import MMIFMaster
from waveflow.simulation.simulation import Simulation

import pytest

_DemoCmd = IntField.specialize(bitwidth=32, signed=False)


def _queue(comp) -> AXIMMQueue:
    comp.m_mem = MMIFMaster(name=f"{comp.name}_m_mem", sim=comp.sim, bitwidth=64)
    comp.add_endpoint(comp.m_mem)
    layout = AXIMMQueueLayout(
        base_addr=0, capacity=8, elem_words=_DemoCmd.nwords_per_inst(64), mem_bw=64,
    )
    return AXIMMQueue(master=comp.m_mem, layout=layout)


class _VarRhsConsumer(HwModule):
    """Branches on two runtime-read values: ``if a != b``."""

    def __post_init__(self) -> None:
        super().__post_init__()
        self.cmd_queue = _queue(self)

    @property
    def Cmd(self) -> type:
        return _DemoCmd

    def run_proc(self):
        while True:
            a = yield from self.cmd_queue.get(self.Cmd)
            b = yield from self.cmd_queue.get(self.Cmd)
            if a != b:
                return a
            return b


class _ConstRhsConsumer(HwModule):
    """The unchanged constant path: ``if a == 5``."""

    def __post_init__(self) -> None:
        super().__post_init__()
        self.cmd_queue = _queue(self)

    @property
    def Cmd(self) -> type:
        return _DemoCmd

    def run_proc(self):
        while True:
            a = yield from self.cmd_queue.get(self.Cmd)
            if a == 5:
                return a
            return a


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

def test_var_rhs_binds_to_hwvar():
    tree = HwStmtExtractor(_VarRhsConsumer(name="c", sim=Simulation())).extract()
    assert isinstance(tree, WhileStmt)
    body = tree.body.stmts
    get_a, get_b = body[0], body[1]
    case = body[2]
    assert isinstance(case, CaseStmt)
    assert case.op == "!="
    # the rhs is the SAME HwVar bound by the second get (a runtime value),
    # not a literal/AST node.
    assert isinstance(case.value, HwVar)
    assert case.value is get_b.outputs[0]
    assert case.var is get_a.outputs[0]


def test_const_rhs_unregressed():
    tree = HwStmtExtractor(_ConstRhsConsumer(name="c", sim=Simulation())).extract()
    case = tree.body.stmts[1]
    assert isinstance(case, CaseStmt)
    assert case.op == "=="
    assert case.value == 5  # still a plain constant


def test_non_eq_op_still_rejected():
    class _Bad(HwModule):
        def __post_init__(self) -> None:
            super().__post_init__()
            self.cmd_queue = _queue(self)

        @property
        def Cmd(self) -> type:
            return _DemoCmd

        def run_proc(self):
            while True:
                a = yield from self.cmd_queue.get(self.Cmd)
                b = yield from self.cmd_queue.get(self.Cmd)
                if a < b:           # noqa — intentionally unsupported
                    return a
                return b

    with pytest.raises(SynthesisError, match="'==' and '!='"):
        HwStmtExtractor(_Bad(name="c", sim=Simulation())).extract()


# ---------------------------------------------------------------------------
# Emission
# ---------------------------------------------------------------------------

def _ctx():
    return CodegenCtx(comp=HwModule(name="c", sim=Simulation()))


def test_var_rhs_emits_variable_name():
    a = HwVar(name="a", typ=_DemoCmd)
    b = HwVar(name="b", typ=_DemoCmd)
    stmt = CaseStmt(
        var=a, field=None, value=b,
        if_true=SeqStmt(stmts=[ReturnStmt(value=a)]), if_false=None, op="!=",
    )
    out = to_cpp(stmt, _ctx())
    assert out.splitlines()[0] == "    if (a != b) {"
    assert "return a;" in out


def test_var_rhs_field_lhs_and_var_rhs():
    a = HwVar(name="cmd", typ=_DemoCmd)
    n = HwVar(name="limit", typ=_DemoCmd)
    stmt = CaseStmt(
        var=a, field="count", value=n,
        if_true=SeqStmt(stmts=[ReturnStmt(value=a)]), if_false=None, op="==",
    )
    assert to_cpp(stmt, _ctx()).splitlines()[0] == "    if (cmd.count == limit) {"


def test_const_rhs_emits_literal():
    a = HwVar(name="a", typ=_DemoCmd)
    stmt = CaseStmt(
        var=a, field="op", value=5,
        if_true=SeqStmt(stmts=[ReturnStmt(value=a)]), if_false=None, op="==",
    )
    assert to_cpp(stmt, _ctx()).splitlines()[0] == "    if (a.op == 5) {"
