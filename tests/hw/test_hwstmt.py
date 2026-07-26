from __future__ import annotations

import pytest

from waveflow.build.hwcodegen import HwStmtExtractor, SynthesisError
from waveflow.hw.hw_module import HwModule
from waveflow.hw.hwstmt import (
    ContinueStmt,
    FunctionStmt,
    HwVar,
    SeqStmt,
    SynthCallStmt,
    WhileStmt,
)
from waveflow.hw.synth import synthesizable
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


# ---------------------------------------------------------------------------
# Helper to build a component with .ep set
# ---------------------------------------------------------------------------

def _make_comp(comp_cls):
    sim = Simulation()
    comp = comp_cls(sim=sim)
    comp.ep = _MockEndpoint()
    return comp


# ---------------------------------------------------------------------------
# Basic while-True + get + write
# ---------------------------------------------------------------------------

class _EchoComp(HwModule):
    def run_proc(self):
        while True:
            x = yield from self.ep.get()
            yield from self.ep.write(x)


def test_extract_while_stmt_is_root():
    comp = _make_comp(_EchoComp)
    tree = HwStmtExtractor(comp).extract()
    assert isinstance(tree, WhileStmt)


def test_extract_while_body_is_seq():
    comp = _make_comp(_EchoComp)
    tree = HwStmtExtractor(comp).extract()
    assert isinstance(tree.body, SeqStmt)
    assert len(tree.body.stmts) == 2


def test_extract_get_produces_synth_call():
    comp = _make_comp(_EchoComp)
    tree = HwStmtExtractor(comp).extract()
    get_stmt = tree.body.stmts[0]
    assert isinstance(get_stmt, SynthCallStmt)


def test_extract_write_produces_synth_call():
    comp = _make_comp(_EchoComp)
    tree = HwStmtExtractor(comp).extract()
    write_stmt = tree.body.stmts[1]
    assert isinstance(write_stmt, SynthCallStmt)


def test_extract_get_output_bound_as_hwvar():
    comp = _make_comp(_EchoComp)
    tree = HwStmtExtractor(comp).extract()
    get_stmt = tree.body.stmts[0]
    assert len(get_stmt.outputs) == 1
    assert isinstance(get_stmt.outputs[0], HwVar)
    assert get_stmt.outputs[0].name == 'x'


def test_extract_write_passes_hwvar_as_input():
    comp = _make_comp(_EchoComp)
    tree = HwStmtExtractor(comp).extract()
    write_stmt = tree.body.stmts[1]
    assert len(write_stmt.inputs) == 1
    assert isinstance(write_stmt.inputs[0], HwVar)
    assert write_stmt.inputs[0].name == 'x'


def test_extract_output_hwvar_producer_is_stmt():
    comp = _make_comp(_EchoComp)
    tree = HwStmtExtractor(comp).extract()
    get_stmt = tree.body.stmts[0]
    assert get_stmt.outputs[0].producer is get_stmt


# ---------------------------------------------------------------------------
# Hook (no synth_fn) method
# ---------------------------------------------------------------------------

class _HookComp(HwModule):
    @synthesizable
    def compute(self, x):
        return x

    def run_proc(self):
        while True:
            x = yield from self.ep.get()
            y = self.compute(x)
            yield from self.ep.write(y)


def test_extract_hook_produces_function_stmt():
    comp = _make_comp(_HookComp)
    tree = HwStmtExtractor(comp).extract()
    compute_stmt = tree.body.stmts[1]
    assert isinstance(compute_stmt, FunctionStmt)
    assert compute_stmt.outputs[0].name == 'y'


def test_extract_hook_input_is_hwvar():
    comp = _make_comp(_HookComp)
    tree = HwStmtExtractor(comp).extract()
    compute_stmt = tree.body.stmts[1]
    assert isinstance(compute_stmt.inputs[0], HwVar)
    assert compute_stmt.inputs[0].name == 'x'


# ---------------------------------------------------------------------------
# ContinueStmt inside while True
# ---------------------------------------------------------------------------

class _ContinueComp(HwModule):
    def run_proc(self):
        while True:
            x = yield from self.ep.get()
            yield from self.ep.write(x)
            continue


def test_extract_continue_stmt():
    comp = _make_comp(_ContinueComp)
    tree = HwStmtExtractor(comp).extract()
    assert isinstance(tree, WhileStmt)
    last = tree.body.stmts[-1]
    assert isinstance(last, ContinueStmt)


# ---------------------------------------------------------------------------
# Bare for-loop raises SynthesisError
# ---------------------------------------------------------------------------

class _ForLoopComp(HwModule):
    def run_proc(self):
        for i in range(10):
            pass


def test_for_loop_raises_synthesis_error():
    sim = Simulation()
    comp = _ForLoopComp(sim=sim)
    with pytest.raises(SynthesisError, match="(?i)for"):
        HwStmtExtractor(comp).extract()


# ---------------------------------------------------------------------------
# Non-synthesizable method call raises SynthesisError
# ---------------------------------------------------------------------------

class _NonSynthComp(HwModule):
    def plain_method(self):
        pass

    def run_proc(self):
        while True:
            self.plain_method()


def test_non_synthesizable_call_raises():
    sim = Simulation()
    comp = _NonSynthComp(sim=sim)
    with pytest.raises(SynthesisError):
        HwStmtExtractor(comp).extract()


# ---------------------------------------------------------------------------
# Non-True while condition raises SynthesisError
# ---------------------------------------------------------------------------

class _WhileCondComp(HwModule):
    def run_proc(self):
        while False:
            pass


def test_while_non_true_raises():
    sim = Simulation()
    comp = _WhileCondComp(sim=sim)
    with pytest.raises(SynthesisError, match="while True"):
        HwStmtExtractor(comp).extract()


# ---------------------------------------------------------------------------
# Top-level SeqStmt when run_proc has no while
# ---------------------------------------------------------------------------

class _SeqComp(HwModule):
    def run_proc(self):
        x = yield from self.ep.get()
        yield from self.ep.write(x)


def test_flat_run_proc_produces_seq_stmt():
    comp = _make_comp(_SeqComp)
    tree = HwStmtExtractor(comp).extract()
    assert isinstance(tree, SeqStmt)
    assert len(tree.stmts) == 2
