"""Tests for the IR resolution pass (waveflow/build/hwresolve.py)."""
from __future__ import annotations

import ast
from dataclasses import dataclass
from enum import IntEnum

import pytest

from waveflow.build.hwcodegen import HwStmtExtractor
from waveflow.hw.dataschema import DataList, EnumField, IntField
from waveflow.hw.hw_module import HwModule, HwParam
from waveflow.hw.hwstmt import (
    CaseStmt,
    FieldRef,
    HwVar,
    SeqStmt,
    WhileStmt,
)
from waveflow.hw.interface import StreamIFMaster, StreamIFSlave
from waveflow.hw.regmap import (
    Bit,
    RegAccess,
    RegField,
    VitisRegMap,
    VitisRegMapMMIFSlave,
)
from waveflow.hw.synth import sim_only, synthesizable
from waveflow.simulation.simobj import ProcessGen
from waveflow.simulation.simulation import Simulation


# ---------------------------------------------------------------------------
# Module-level schemas / enums used by the demo component
# ---------------------------------------------------------------------------

class DemoCmdType(IntEnum):
    DATA = 0
    END  = 1


class DemoError(IntEnum):
    OK         = 0
    BAD_INPUT  = 1
    OVERFLOW   = 2


DemoCmdTypeField = EnumField.specialize(enum_type=DemoCmdType)
DemoErrorField   = EnumField.specialize(enum_type=DemoError)
TxIdField        = IntField.specialize(bitwidth=16, signed=False)


class DemoCmdHdr(DataList):
    elements = {
        "cmd_type": {"schema": DemoCmdTypeField, "description": "DATA or END"},
        "tx_id":    {"schema": TxIdField,        "description": "Transaction ID"},
    }


# ---------------------------------------------------------------------------
# Demo component (mirrors experiment/extract_demo.py)
# ---------------------------------------------------------------------------

@dataclass
class Demo(HwModule):
    in_bw:  HwParam[int] = 32
    out_bw: HwParam[int] = 32

    def __post_init__(self) -> None:
        super().__post_init__()
        self.s_in  = StreamIFSlave (name=f'{self.name}_s_in',  sim=self.sim, bitwidth=self.in_bw)
        self.m_out = StreamIFMaster(name=f'{self.name}_m_out', sim=self.sim, bitwidth=self.out_bw)
        self.regmap = VitisRegMap({
            "halted": RegField(Bit,            RegAccess.R),
            "error":  RegField(DemoErrorField, RegAccess.R),
            "tx_id":  RegField(TxIdField,      RegAccess.R),
        })
        self.s_lite = VitisRegMapMMIFSlave(
            name=f'{self.name}_s_lite', sim=self.sim, bitwidth=32,
            regmap=self.regmap, on_start=self.on_start,
        )
        for ep in (self.s_in, self.m_out, self.s_lite):
            self.add_endpoint(ep)

    @sim_only
    def log(self, msg: str) -> None:
        pass

    def on_start(self) -> ProcessGen[None]:
        while True:
            self.log("waiting")
            cmd: DemoCmdHdr = yield from self.s_in.get_schema(DemoCmdHdr)
            if cmd.cmd_type == DemoCmdType.END:
                return
            err = yield from self.process(cmd)
            if err != DemoError.OK:
                self.regmap.set("error",  err)
                self.regmap.set("tx_id",  cmd.tx_id)
                self.regmap.set("halted", 1)
                return

    @synthesizable
    def process(self, cmd: DemoCmdHdr) -> ProcessGen[DemoError]:
        yield self.env.timeout(0)
        return DemoError.OK


def _make_demo() -> Demo:
    return Demo(name="demo", sim=Simulation())


# ---------------------------------------------------------------------------
# Phase 1: CaseStmt.field becomes Optional[str]
# ---------------------------------------------------------------------------

def test_bare_var_case_stmt_has_none_field():
    """`if err == DemoError.OK:` — left side is a bare HwVar, no field."""
    comp = _make_demo()
    tree = HwStmtExtractor(comp, method_name='on_start').extract()
    assert isinstance(tree, WhileStmt)
    body = tree.body.stmts
    # body[3] is `if err != DemoError.OK:` in the demo on_start.
    case = body[3]
    assert isinstance(case, CaseStmt)
    assert case.field is None
    assert case.op == '!='


def test_field_case_stmt_has_field_name():
    """`if cmd.cmd_type == DemoCmdType.END:` — left side is var.field."""
    comp = _make_demo()
    tree = HwStmtExtractor(comp, method_name='on_start').extract()
    body = tree.body.stmts
    case = body[1]
    assert isinstance(case, CaseStmt)
    assert case.field == 'cmd_type'
    assert case.op == '=='


# ---------------------------------------------------------------------------
# Phase 2: Input resolution
# ---------------------------------------------------------------------------

def _extract_and_resolve(comp: HwModule, method_name: str = 'on_start'):
    from waveflow.build.hwresolve import resolve_kernel
    tree = HwStmtExtractor(comp, method_name=method_name).extract()
    return resolve_kernel(tree, comp)


def test_resolve_stream_get_class_input():
    """`s_in.get(DemoCmdHdr)` — DemoCmdHdr Name resolves to the class."""
    comp = _make_demo()
    tree = _extract_and_resolve(comp)
    body = tree.body.stmts
    get_stmt = body[0]
    assert get_stmt.inputs[0] is DemoCmdHdr


def test_resolve_regmap_set_constant_and_hwvar():
    """`self.regmap.set("error", err)` — string + HwVar."""
    comp = _make_demo()
    tree = _extract_and_resolve(comp)
    body = tree.body.stmts
    case_err = body[3]
    set_error = case_err.if_true.stmts[0]
    assert set_error.inputs[0] == "error"
    assert isinstance(set_error.inputs[1], HwVar)
    assert set_error.inputs[1].name == "err"


def test_resolve_regmap_set_field_ref():
    """`self.regmap.set("tx_id", cmd.tx_id)` — cmd.tx_id becomes FieldRef."""
    comp = _make_demo()
    tree = _extract_and_resolve(comp)
    body = tree.body.stmts
    case_err = body[3]
    set_tx_id = case_err.if_true.stmts[1]
    assert set_tx_id.inputs[0] == "tx_id"
    arg = set_tx_id.inputs[1]
    assert isinstance(arg, FieldRef)
    assert arg.var.name == "cmd"
    assert arg.field == "tx_id"


def test_resolve_case_stmt_value_is_enum_member():
    """`if err != DemoError.OK` — value becomes the enum member, not a string."""
    comp = _make_demo()
    tree = _extract_and_resolve(comp)
    body = tree.body.stmts
    case_err = body[3]
    assert case_err.value is DemoError.OK


def test_resolve_case_stmt_field_value_is_enum_member():
    """`if cmd.cmd_type == DemoCmdType.END` — value resolves to enum member."""
    comp = _make_demo()
    tree = _extract_and_resolve(comp)
    body = tree.body.stmts
    case_end = body[1]
    assert case_end.value is DemoCmdType.END


def test_resolve_unresolved_name_raises():
    """Reference to a name that doesn't exist anywhere triggers ResolutionError."""
    from waveflow.build.hwresolve import ResolutionError, resolve_kernel

    @dataclass
    class _BadDemo(HwModule):
        def __post_init__(self) -> None:
            super().__post_init__()
            self.s_in = StreamIFSlave(
                name=f'{self.name}_s_in', sim=self.sim, bitwidth=32,
            )
            self.add_endpoint(self.s_in)

        def run_proc(self) -> ProcessGen[None]:
            while True:
                yield from self.s_in.get_schema(NotARealClass)  # noqa: F821

    comp = _BadDemo(name="bad", sim=Simulation())
    tree = HwStmtExtractor(comp, method_name='run_proc').extract()
    with pytest.raises(ResolutionError, match="NotARealClass"):
        resolve_kernel(tree, comp)


# ---------------------------------------------------------------------------
# Phase 3: HwVar.typ population
# ---------------------------------------------------------------------------

def test_stream_get_output_typ_is_schema_class():
    comp = _make_demo()
    tree = _extract_and_resolve(comp)
    cmd_var = tree.body.stmts[0].outputs[0]
    assert cmd_var.name == 'cmd'
    assert cmd_var.typ is DemoCmdHdr


def test_function_stmt_output_typ_unwraps_process_gen():
    comp = _make_demo()
    tree = _extract_and_resolve(comp)
    err_var = tree.body.stmts[2].outputs[0]
    assert err_var.name == 'err'
    assert err_var.typ is DemoError


def test_regmap_get_output_typ_is_field_schema():
    """A component that reads a regmap field gets a typed HwVar."""
    from waveflow.hw.dataschema import FloatField

    Float32 = FloatField.specialize(bitwidth=32)

    @dataclass
    class _RegMapReadDemo(HwModule):
        def __post_init__(self) -> None:
            super().__post_init__()
            self.s_in = StreamIFSlave(
                name=f'{self.name}_s_in', sim=self.sim, bitwidth=32,
            )
            self.regmap = VitisRegMap({
                "gain": RegField(Float32, RegAccess.RW),
            })
            self.s_lite = VitisRegMapMMIFSlave(
                name=f'{self.name}_s_lite', sim=self.sim, bitwidth=32,
                regmap=self.regmap, on_start=self.on_start,
            )
            for ep in (self.s_in, self.s_lite):
                self.add_endpoint(ep)

        def on_start(self) -> ProcessGen[None]:
            while True:
                gain = self.regmap.get("gain")  # noqa: F841 — used by test
                yield from self.s_in.get_schema(DemoCmdHdr)
                return

    comp = _RegMapReadDemo(name="rmr", sim=Simulation())
    tree = _extract_and_resolve(comp)
    gain_stmt = tree.body.stmts[0]
    assert gain_stmt.outputs[0].name == 'gain'
    assert gain_stmt.outputs[0].typ is Float32


def test_every_hwvar_has_typ_after_resolution():
    comp = _make_demo()
    tree = _extract_and_resolve(comp)
    seen: list[HwVar] = []

    def _collect(stmt):
        if isinstance(stmt, WhileStmt):
            _collect(stmt.body)
        elif isinstance(stmt, SeqStmt):
            for s in stmt.stmts:
                _collect(s)
        elif isinstance(stmt, CaseStmt):
            _collect(stmt.if_true)
            if stmt.if_false is not None:
                _collect(stmt.if_false)
        elif hasattr(stmt, 'outputs'):
            seen.extend(stmt.outputs)

    _collect(tree)
    assert seen, "expected at least one HwVar in the resolved tree"
    for v in seen:
        assert v.typ is not None, f"HwVar {v.name!r} has no typ after resolution"


def test_resolve_no_ast_nodes_in_inputs():
    """Walking the resolved tree should find no ast.AST nodes in any inputs."""
    comp = _make_demo()
    tree = _extract_and_resolve(comp)

    def _check(stmt):
        if isinstance(stmt, WhileStmt):
            _check(stmt.body)
        elif isinstance(stmt, SeqStmt):
            for s in stmt.stmts:
                _check(s)
        elif isinstance(stmt, CaseStmt):
            assert not isinstance(stmt.value, ast.AST), \
                f"CaseStmt.value still an ast node: {ast.dump(stmt.value)}"
            _check(stmt.if_true)
            if stmt.if_false is not None:
                _check(stmt.if_false)
        elif hasattr(stmt, 'inputs'):
            for x in stmt.inputs:
                assert not isinstance(x, ast.AST), \
                    f"Unresolved ast node in inputs: {ast.dump(x)}"

    _check(tree)


# ---------------------------------------------------------------------------
# Phase 4: extract_kernel returns a resolved tree
# ---------------------------------------------------------------------------

def test_extract_kernel_returns_resolved_tree():
    """extract_kernel(comp) returns a tree with no ast.AST nodes in inputs."""
    from waveflow.build.hwcodegen import extract_kernel
    comp = _make_demo()
    tree = extract_kernel(comp)

    def _check(stmt):
        if isinstance(stmt, WhileStmt):
            _check(stmt.body)
        elif isinstance(stmt, SeqStmt):
            for s in stmt.stmts:
                _check(s)
        elif isinstance(stmt, CaseStmt):
            assert not isinstance(stmt.value, ast.AST)
            _check(stmt.if_true)
            if stmt.if_false is not None:
                _check(stmt.if_false)
        elif hasattr(stmt, 'inputs'):
            for x in stmt.inputs:
                assert not isinstance(x, ast.AST)

    _check(tree)


def test_extract_kernel_populates_hwvar_types():
    """Every HwVar emitted by a recognised statement has a non-None typ."""
    from waveflow.build.hwcodegen import extract_kernel
    comp = _make_demo()
    tree = extract_kernel(comp)

    seen: list[HwVar] = []

    def _collect(stmt):
        if isinstance(stmt, WhileStmt):
            _collect(stmt.body)
        elif isinstance(stmt, SeqStmt):
            for s in stmt.stmts:
                _collect(s)
        elif isinstance(stmt, CaseStmt):
            _collect(stmt.if_true)
            if stmt.if_false is not None:
                _collect(stmt.if_false)
        elif hasattr(stmt, 'outputs'):
            seen.extend(stmt.outputs)

    _collect(tree)
    assert seen
    for v in seen:
        assert v.typ is not None, f"HwVar {v.name!r} has no typ"
