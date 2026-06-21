"""End-to-end extractor test for VmacAccel.run_proc (Phase 2).

Companion to ``tests/hw/test_extract_poly.py`` (which targets poly's ``on_start``): VMAC has no
regmap, so its kernel is ``run_proc`` (decision D1).  This asserts that ``HwStmtExtractor``
lowers the free-running command-queue consumer to a clean ``HwStmt`` tree — the ring dequeue,
the ``end`` sentinel, and the datapath hook — with the ``@sim_only`` bookkeeping dropped.
"""
from __future__ import annotations

from examples.vmac.vmac import VmacAccel
from examples.vmac.vmac_datatypes import OpCode
from waveflow.build.hwcodegen import HwStmtExtractor, extract_kernel
from waveflow.hw.aximm_queue import (
    AXIMMQueue,
    AXIMMQueueGetStmt,
    AXIMMQueueLayout,
)
from waveflow.hw.hwstmt import CaseStmt, FunctionStmt, ReturnStmt, WhileStmt
from waveflow.simulation.simulation import Simulation


def _accel_with_queue(mem_bw: int = 64) -> VmacAccel:
    """A sim-mode VmacAccel with a command queue attached (as the driver does before run_sim)."""
    accel = VmacAccel(name="vmac", sim=Simulation(), mem_dwidth=mem_bw)
    layout = AXIMMQueueLayout(
        base_addr=0, capacity=8,
        elem_words=accel.Cmd.nwords_per_inst(mem_bw), mem_bw=mem_bw,
    )
    accel.cmd_queue = AXIMMQueue(master=accel.m_mem, layout=layout)
    return accel


def test_run_proc_extracts_to_while():
    tree = HwStmtExtractor(_accel_with_queue()).extract()
    assert isinstance(tree, WhileStmt)


def test_run_proc_body_shape():
    """Expected body (the two @sim_only records dropped):
        0: cmd = yield from self.cmd_queue.get(self.Cmd)   -> AXIMMQueueGetStmt
        1: if cmd.op == OpCode.end: return                  -> CaseStmt(op='==') + ReturnStmt
        2: yield from self.vmac_compute(cmd, self.m_mem)    -> FunctionStmt
    """
    tree = HwStmtExtractor(_accel_with_queue()).extract()
    body = tree.body.stmts
    assert len(body) == 3

    assert isinstance(body[0], AXIMMQueueGetStmt)
    assert body[0].outputs[0].name == "cmd"

    assert isinstance(body[1], CaseStmt) and body[1].op == "=="
    assert body[1].field == "op"
    end_branch = body[1].if_true.stmts
    assert any(isinstance(s, ReturnStmt) for s in end_branch)

    assert isinstance(body[2], FunctionStmt)
    assert body[2].method.__name__ == "vmac_compute"
    assert body[2].impl_file == "vmac_compute_impl.tpp"


def test_run_proc_end_sentinel_compares_against_opcode_end():
    tree = HwStmtExtractor(_accel_with_queue()).extract()
    case_stmt = tree.body.stmts[1]
    # The compared value resolves to OpCode.end after kernel resolution; pre-resolution it is the
    # OpCode.end reference.  Assert the field is the op selector and the op is equality.
    assert case_stmt.field == "op"
    assert case_stmt.op == "=="


def test_sim_only_records_are_dropped():
    # _record_dequeue / _record_command are @sim_only: they must not appear in the IR.
    tree = HwStmtExtractor(_accel_with_queue()).extract()
    kinds = [type(s).__name__ for s in tree.body.stmts]
    assert "SynthCallStmt" not in kinds  # no stray sim-only calls
    assert kinds == ["AXIMMQueueGetStmt", "CaseStmt", "FunctionStmt"]


def test_extract_kernel_no_regmap_uses_run_proc():
    # VMAC has no VitisRegMapMMIFSlave endpoint, so the kernel policy picks run_proc (D1).
    tree = extract_kernel(_accel_with_queue())
    assert isinstance(tree, WhileStmt)
    assert isinstance(tree.body.stmts[0], AXIMMQueueGetStmt)


def test_extract_kernel_resolves_end_sentinel_value():
    # After resolve_kernel, the CaseStmt value is the real OpCode.end enum member.
    tree = extract_kernel(_accel_with_queue())
    case_stmt = tree.body.stmts[1]
    assert case_stmt.value == OpCode.end
