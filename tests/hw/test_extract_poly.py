"""End-to-end extractor tests targeting the PolyAccel kernel."""
from __future__ import annotations

import pytest

from waveflow.build.hwcodegen import HwStmtExtractor, SynthesisError
from waveflow.hw.hw_module import HwModule
from waveflow.hw.hwstmt import (
    CaseStmt,
    FunctionStmt,
    ReturnStmt,
    SeqStmt,
    WhileStmt,
)
from waveflow.hw.synth import sim_only, synthesizable
from waveflow.simulation.simulation import Simulation


# ---------------------------------------------------------------------------
# Minimal synthesizable endpoint for testing
# ---------------------------------------------------------------------------

class _MockEndpoint:
    """Stand-in for a stream endpoint with synthesizable get/write."""

    @synthesizable(synth_fn=lambda ctx, i, o: "")
    def get(self):
        pass

    @synthesizable(synth_fn=lambda ctx, i, o: "")
    def write(self, data):
        pass


def _make_comp(comp_cls):
    sim = Simulation()
    comp = comp_cls(sim=sim)
    comp.ep = _MockEndpoint()
    return comp


# ---------------------------------------------------------------------------
# Phase 1: ReturnStmt
# ---------------------------------------------------------------------------

class _ReturnInIfComp(HwModule):
    def run_proc(self):
        while True:
            x = yield from self.ep.get()
            if x.f == 1:
                return


def test_return_inside_if_body():
    comp = _make_comp(_ReturnInIfComp)
    tree = HwStmtExtractor(comp).extract()
    assert isinstance(tree, WhileStmt)
    case_stmt = tree.body.stmts[1]
    assert isinstance(case_stmt, CaseStmt)
    first_in_branch = case_stmt.if_true.stmts[0]
    assert isinstance(first_in_branch, ReturnStmt)
    assert first_in_branch.value is None


class _ReturnAtTopComp(HwModule):
    def run_proc(self):
        while True:
            yield from self.ep.get()
            return


def test_return_at_top_of_while():
    comp = _make_comp(_ReturnAtTopComp)
    tree = HwStmtExtractor(comp).extract()
    assert isinstance(tree, WhileStmt)
    assert isinstance(tree.body, SeqStmt)
    assert any(isinstance(s, ReturnStmt) for s in tree.body.stmts)


# ---------------------------------------------------------------------------
# Phase 2: != in CaseStmt
# ---------------------------------------------------------------------------

class _NotEqIfComp(HwModule):
    def run_proc(self):
        while True:
            x = yield from self.ep.get()
            if x.f != 0:
                return


def test_case_stmt_not_eq_op():
    comp = _make_comp(_NotEqIfComp)
    tree = HwStmtExtractor(comp).extract()
    case_stmt = tree.body.stmts[1]
    assert isinstance(case_stmt, CaseStmt)
    assert case_stmt.op == '!='


class _EqIfComp(HwModule):
    def run_proc(self):
        while True:
            x = yield from self.ep.get()
            if x.f == 0:
                return


def test_case_stmt_eq_op_default():
    comp = _make_comp(_EqIfComp)
    tree = HwStmtExtractor(comp).extract()
    case_stmt = tree.body.stmts[1]
    assert isinstance(case_stmt, CaseStmt)
    assert case_stmt.op == '=='


# ---------------------------------------------------------------------------
# Phase 3: extract_kernel policy helper
# ---------------------------------------------------------------------------

def test_extract_kernel_no_regmap_uses_run_proc():
    from waveflow.build.hwcodegen import extract_kernel
    comp = _make_comp(_EqIfComp)
    tree = extract_kernel(comp)
    assert isinstance(tree, WhileStmt)


# ---------------------------------------------------------------------------
# Phase 4: No-implicit-capture rule
# ---------------------------------------------------------------------------

class _CaptureProcLatencyComp(HwModule):
    proc_latency: int = 10

    def run_proc(self):
        while True:
            x = yield from self.ep.get()
            _ = self.proc_latency  # implicit capture — should error
            yield from self.ep.write(x)


def test_implicit_capture_plain_field_raises():
    comp = _make_comp(_CaptureProcLatencyComp)
    with pytest.raises(SynthesisError, match="proc_latency"):
        HwStmtExtractor(comp).extract()


class _SimOnlyLoggerStub:
    @sim_only
    def log(self, **kwargs):
        pass


class _SimOnlyCallComp(HwModule):
    def run_proc(self):
        while True:
            self.logger.log(event='proc_begin')
            x = yield from self.ep.get()
            yield from self.ep.write(x)


def test_sim_only_chain_does_not_raise():
    sim = Simulation()
    comp = _SimOnlyCallComp(sim=sim)
    comp.ep = _MockEndpoint()
    comp.logger = _SimOnlyLoggerStub()
    tree = HwStmtExtractor(comp).extract()
    assert isinstance(tree, WhileStmt)
    # The @sim_only call is dropped: body is [get, write], not [log, get, write].
    assert len(tree.body.stmts) == 2


class _SynthEndpointCallComp(HwModule):
    def run_proc(self):
        while True:
            x = yield from self.ep.get()
            yield from self.ep.write(x)


def test_synth_endpoint_call_does_not_raise():
    comp = _make_comp(_SynthEndpointCallComp)
    tree = HwStmtExtractor(comp).extract()
    assert isinstance(tree, WhileStmt)


# ---------------------------------------------------------------------------
# Phase 5: RegMap get/set are synthesizable
# ---------------------------------------------------------------------------

def test_regmap_set_produces_regmap_set_stmt():
    from waveflow.hw.dataschema import IntField
    from waveflow.hw.regmap import (
        RegAccess, RegField, RegMapSetStmt, VitisRegMap,
    )

    class _RegMapWriteComp(HwModule):
        def __post_init__(self):
            super().__post_init__()
            self.regmap = VitisRegMap({
                "error": RegField(IntField.specialize(bitwidth=8, signed=False),
                                  RegAccess.R),
            })

        def run_proc(self):
            while True:
                v = yield from self.ep.get()
                self.regmap.set("error", v)

    sim = Simulation()
    comp = _RegMapWriteComp(sim=sim)
    comp.ep = _MockEndpoint()
    tree = HwStmtExtractor(comp).extract()
    assert isinstance(tree, WhileStmt)
    set_stmt = tree.body.stmts[1]
    assert isinstance(set_stmt, RegMapSetStmt)
    # First input is the "error" field name (as ast.Constant); second is the HwVar `v`.
    import ast as _ast
    from waveflow.hw.hwstmt import HwVar
    assert isinstance(set_stmt.inputs[0], _ast.Constant)
    assert set_stmt.inputs[0].value == "error"
    assert isinstance(set_stmt.inputs[1], HwVar)
    assert set_stmt.inputs[1].name == "v"


def test_regmap_get_produces_regmap_get_stmt():
    from waveflow.hw.dataschema import IntField
    from waveflow.hw.regmap import (
        RegAccess, RegField, RegMapGetStmt, VitisRegMap,
    )

    class _RegMapReadComp(HwModule):
        def __post_init__(self):
            super().__post_init__()
            self.regmap = VitisRegMap({
                "coeffs": RegField(IntField.specialize(bitwidth=8, signed=False),
                                   RegAccess.RW),
            })

        def run_proc(self):
            while True:
                coeffs = self.regmap.get("coeffs")
                yield from self.ep.write(coeffs)

    sim = Simulation()
    comp = _RegMapReadComp(sim=sim)
    comp.ep = _MockEndpoint()
    tree = HwStmtExtractor(comp).extract()
    assert isinstance(tree, WhileStmt)
    get_stmt = tree.body.stmts[0]
    assert isinstance(get_stmt, RegMapGetStmt)
    # Output `coeffs` was bound as a HwVar; the write call references it.
    assert len(get_stmt.outputs) == 1
    assert get_stmt.outputs[0].name == "coeffs"


# ---------------------------------------------------------------------------
# Phase 6: FunctionStmt for user-written @synthesizable methods
# ---------------------------------------------------------------------------

class _UserMethodComp(HwModule):
    @synthesizable
    def evaluate(self, x):
        yield None
        return x

    def run_proc(self):
        while True:
            x = yield from self.ep.get()
            y = yield from self.evaluate(x)
            yield from self.ep.write(y)


def test_user_method_produces_function_stmt():
    comp = _make_comp(_UserMethodComp)
    tree = HwStmtExtractor(comp).extract()
    eval_stmt = tree.body.stmts[1]
    assert isinstance(eval_stmt, FunctionStmt)
    assert eval_stmt.method.__name__ == 'evaluate'
    assert eval_stmt.impl_file is None


class _UserMethodImplFileComp(HwModule):
    @synthesizable(impl_file="custom.cpp")
    def evaluate(self, x):
        yield None
        return x

    def run_proc(self):
        while True:
            x = yield from self.ep.get()
            y = yield from self.evaluate(x)
            yield from self.ep.write(y)


def test_user_method_impl_file_propagates():
    comp = _make_comp(_UserMethodImplFileComp)
    tree = HwStmtExtractor(comp).extract()
    eval_stmt = tree.body.stmts[1]
    assert isinstance(eval_stmt, FunctionStmt)
    assert eval_stmt.impl_file == "custom.cpp"


def test_user_method_inputs_resolve_endpoint_reference():
    from waveflow.hw.interface import StreamIFSlave

    class _UserMethodWithEndpointComp(HwModule):
        def __post_init__(self):
            super().__post_init__()
            self.s_in = StreamIFSlave(
                name=f'{self.name}_s_in', sim=self.sim, bitwidth=32,
            )
            self.add_endpoint(self.s_in)

        @synthesizable
        def evaluate(self, x, ep):
            yield None
            return x

        def run_proc(self):
            while True:
                x = yield from self.s_in.get()
                yield from self.evaluate(x, self.s_in)

    sim = Simulation()
    comp = _UserMethodWithEndpointComp(sim=sim)
    tree = HwStmtExtractor(comp).extract()
    eval_stmt = tree.body.stmts[1]
    assert isinstance(eval_stmt, FunctionStmt)
    # First arg is the HwVar `x`; second is the resolved endpoint object.
    from waveflow.hw.hwstmt import HwVar
    assert isinstance(eval_stmt.inputs[0], HwVar)
    assert eval_stmt.inputs[0].name == 'x'
    assert eval_stmt.inputs[1] is comp.s_in


def test_extract_kernel_with_regmap_uses_on_start():
    """A component with a VitisRegMapMMIFSlave endpoint extracts on_start."""
    from waveflow.build.hwcodegen import extract_kernel
    from waveflow.hw.interface import StreamIFSlave
    from waveflow.hw.regmap import (
        Bit, RegAccess, RegField, VitisRegMap, VitisRegMapMMIFSlave,
    )

    class _RegMapComp(HwModule):
        def __post_init__(self):
            super().__post_init__()
            self.s_in = StreamIFSlave(
                name=f'{self.name}_s_in', sim=self.sim, bitwidth=32,
            )
            self.regmap = VitisRegMap({
                "halted": RegField(Bit, RegAccess.R),
            })
            self.s_lite = VitisRegMapMMIFSlave(
                name=f'{self.name}_s_lite', sim=self.sim, bitwidth=32,
                regmap=self.regmap, on_start=self.on_start,
            )
            for ep in (self.s_in, self.s_lite):
                self.add_endpoint(ep)

        def run_proc(self):
            while True:
                yield self.timeout(0)

        def on_start(self):
            while True:
                yield from self.s_in.get()
                return

    sim = Simulation()
    comp = _RegMapComp(sim=sim)
    tree = extract_kernel(comp)
    assert isinstance(tree, WhileStmt)
    # on_start ends with a return; run_proc body would have produced a
    # yield self.timeout(0) which is not synthesizable.
    assert any(isinstance(s, ReturnStmt) for s in tree.body.stmts)


# ---------------------------------------------------------------------------
# Phase 7: End-to-end extraction of PolyAccel.on_start
# ---------------------------------------------------------------------------

def _ensure_poly_on_path():
    import sys
    from pathlib import Path
    POLY_DIR = Path(__file__).resolve().parents[2] / "examples" / "stream_inband"
    if str(POLY_DIR) not in sys.path:
        sys.path.insert(0, str(POLY_DIR))


def test_extract_poly_accel_on_start():
    _ensure_poly_on_path()
    from poly import PolyAccel
    from waveflow.build.hwcodegen import extract_kernel
    from waveflow.hw.interface import StreamGetStmt
    from waveflow.hw.regmap import RegMapGetStmt, RegMapSetStmt

    comp = PolyAccel(name='p', sim=Simulation())
    tree = extract_kernel(comp)

    # Top level: WhileStmt with a SeqStmt body
    assert isinstance(tree, WhileStmt)
    body = tree.body.stmts

    # Expected (logging / _inc_job dropped via @sim_only):
    #   0: cmd_hdr = yield from self.s_in.get_schema(PolyCmdHdr)   → StreamGetStmt
    #   1: if cmd_hdr.cmd_type == PolyCmdType.END: return    → CaseStmt(op='==')
    #   2: coeffs = self.regmap.get("coeffs")                → RegMapGetStmt
    #   3: err = yield from self.evaluate(...)              → FunctionStmt
    #   4: if err != PolyError.NO_ERROR: ...                → CaseStmt(op='!=')
    assert isinstance(body[0], StreamGetStmt)

    assert isinstance(body[1], CaseStmt) and body[1].op == '=='
    end_branch = body[1].if_true.stmts
    assert any(isinstance(s, ReturnStmt) for s in end_branch)

    assert isinstance(body[2], RegMapGetStmt)

    assert isinstance(body[3], FunctionStmt)
    assert body[3].method.__name__ == 'evaluate'

    assert isinstance(body[4], CaseStmt) and body[4].op == '!='
    halt_branch = body[4].if_true.stmts
    regmap_sets = [s for s in halt_branch if isinstance(s, RegMapSetStmt)]
    assert len(regmap_sets) == 3       # error, tx_id, halted
    assert any(isinstance(s, ReturnStmt) for s in halt_branch)


def test_extract_poly_accel_no_implicit_capture_violation():
    """Cloning PolyAccel and adding a self.proc_latency read in
    on_start must raise SynthesisError mentioning proc_latency."""
    _ensure_poly_on_path()
    from poly import (
        PolyAccel, PolyCmdHdr, PolyCmdType, PolyError,
    )
    from waveflow.build.hwcodegen import extract_kernel

    class _BadPolyAccel(PolyAccel):
        def on_start(self):
            while True:
                cmd_hdr = yield from self.s_in.get_schema(PolyCmdHdr)
                # Implicit capture of plain field — should be rejected.
                _ = self.proc_latency
                if cmd_hdr.cmd_type == PolyCmdType.END:
                    return
                err = yield from self.evaluate(cmd_hdr, self.s_in, self.m_out)
                if err != PolyError.NO_ERROR:
                    self.regmap.set("error",  err)
                    self.regmap.set("tx_id",  cmd_hdr.tx_id)
                    self.regmap.set("halted", 1)
                    return

    comp = _BadPolyAccel(name='bad', sim=Simulation())
    with pytest.raises(SynthesisError, match="proc_latency"):
        extract_kernel(comp)
