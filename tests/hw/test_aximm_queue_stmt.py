"""AXIMMQueue.get lowering — IR extraction + C++ emission (Phase 1).

The typed single-element ``AXIMMQueue.get(schema_type)`` path is synthesizable: it extracts
to an :class:`AXIMMQueueGetStmt` and lowers to a call into the hand-written ring-dequeue hook
(``aximm_queue_impl::queue_get``).  Mirrors ``tests/hw/test_hwstmt.py`` (extraction) and
``tests/hw/test_hwgen.py`` (emission).
"""
from __future__ import annotations

from waveflow.build.hwcodegen import HwStmtExtractor
from waveflow.build.hwgen import CodegenCtx, to_cpp
from waveflow.hw.aximm_queue import (
    AXIMMQueue,
    AXIMMQueueGetStmt,
    AXIMMQueueLayout,
)
from waveflow.hw.dataschema import IntField
from waveflow.hw.hw_component import HwComponent
from waveflow.hw.hwstmt import HwVar, ReturnStmt, WhileStmt
from waveflow.hw.memif import MMIFMaster
from waveflow.simulation.simulation import Simulation

# A trivial DataSchema subclass standing in for a command schema (a schema *class* is what the
# typed get takes; for extraction only its identity/DataSchema-ness matter).
_DemoCmd = IntField.specialize(bitwidth=32, signed=False)


# ---------------------------------------------------------------------------
# Extraction: self.cmd_queue.get(self.Cmd) -> AXIMMQueueGetStmt
# ---------------------------------------------------------------------------

class _QueueConsumer(HwComponent):
    def __post_init__(self) -> None:
        super().__post_init__()
        self.m_mem = MMIFMaster(name=f"{self.name}_m_mem", sim=self.sim, bitwidth=64)
        self.add_endpoint(self.m_mem)
        layout = AXIMMQueueLayout(
            base_addr=0, capacity=8,
            elem_words=_DemoCmd.nwords_per_inst(64), mem_bw=64,
        )
        self.cmd_queue = AXIMMQueue(master=self.m_mem, layout=layout)

    @property
    def Cmd(self) -> type:
        """Instance-specialized schema, read as ``self.Cmd`` (cf. VmacAccel.Cmd)."""
        return _DemoCmd

    def run_proc(self):
        while True:
            cmd = yield from self.cmd_queue.get(self.Cmd)
            return cmd


def _consumer() -> _QueueConsumer:
    return _QueueConsumer(name="qc", sim=Simulation())


def test_get_extracts_to_aximm_queue_get_stmt():
    tree = HwStmtExtractor(_consumer()).extract()
    assert isinstance(tree, WhileStmt)
    get_stmt = tree.body.stmts[0]
    assert isinstance(get_stmt, AXIMMQueueGetStmt)


def test_get_output_bound_as_hwvar():
    tree = HwStmtExtractor(_consumer()).extract()
    get_stmt = tree.body.stmts[0]
    assert len(get_stmt.outputs) == 1
    assert isinstance(get_stmt.outputs[0], HwVar)
    assert get_stmt.outputs[0].name == "cmd"


def test_get_input_is_resolved_schema_class():
    # The self.Cmd schema-type arg resolves to the actual class (read-rule allows a
    # DataSchema-subclass self-read, the instance-specialized analogue of a bare global).
    tree = HwStmtExtractor(_consumer()).extract()
    get_stmt = tree.body.stmts[0]
    assert get_stmt.inputs[0] is _DemoCmd


def test_bare_get_call_has_no_poll_kwarg():
    # D3: the poll lives on the queue, so the extracted call carries no kwargs.
    tree = HwStmtExtractor(_consumer()).extract()
    get_stmt = tree.body.stmts[0]
    assert get_stmt.kwargs == {}


def test_return_after_get():
    tree = HwStmtExtractor(_consumer()).extract()
    assert isinstance(tree.body.stmts[1], ReturnStmt)


# ---------------------------------------------------------------------------
# Emission: AXIMMQueueGetStmt -> ring-dequeue hook call
# ---------------------------------------------------------------------------

class _FakeSchema:
    @classmethod
    def cpp_class_name(cls) -> str:
        return "DemoCmd"


class _FakeEndpoint:
    bitwidth: int = 64


class _FakeQueue:
    """Stand-in AXIMMQueue: only ``.layout`` / ``.master`` are read by the emitter."""
    def __init__(self, layout, master) -> None:
        self.layout = layout
        self.master = master


class _FakeBoundMethod:
    def __init__(self, queue) -> None:
        self.__self__ = queue


def test_aximm_queue_get_emits_hook_call():
    gmem = _FakeEndpoint()
    comp = HwComponent(name="c", sim=Simulation())
    comp.gmem = gmem  # discoverable by _endpoint_name via vars(comp)
    layout = AXIMMQueueLayout(base_addr=0, capacity=8, elem_words=2, mem_bw=64)
    queue = _FakeQueue(layout=layout, master=gmem)
    stmt = AXIMMQueueGetStmt(
        method=_FakeBoundMethod(queue),
        inputs=[_FakeSchema],
        outputs=[HwVar(name="cmd", typ=_FakeSchema)],
    )
    expected = (
        "    DemoCmd cmd;\n"
        "    aximm_queue_impl::queue_get<DemoCmd, 64, 0, 8, 2>(gmem, cmd);"
    )
    assert to_cpp(stmt, CodegenCtx(comp=comp)) == expected


def test_aximm_queue_get_emits_layout_geometry():
    # The hook template params come straight from the (non-zero) ring layout.
    gmem = _FakeEndpoint()
    comp = HwComponent(name="c", sim=Simulation())
    comp.gmem = gmem
    layout = AXIMMQueueLayout(base_addr=4096, capacity=16, elem_words=3, mem_bw=64)
    stmt = AXIMMQueueGetStmt(
        method=_FakeBoundMethod(_FakeQueue(layout=layout, master=gmem)),
        inputs=[_FakeSchema],
        outputs=[HwVar(name="cmd", typ=_FakeSchema)],
    )
    out = to_cpp(stmt, CodegenCtx(comp=comp))
    assert "queue_get<DemoCmd, 64, 4096, 16, 3>(gmem, cmd);" in out
