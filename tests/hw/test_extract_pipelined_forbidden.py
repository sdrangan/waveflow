"""Tests: extractor forbids pipelined stream ops in extracted bodies."""
from __future__ import annotations

import pytest

from waveflow.build.hwcodegen import HwStmtExtractor, SynthesisError, extract_kernel
from waveflow.hw.hw_module import HwModule
from waveflow.hw.interface import StreamIFMaster, StreamIFSlave
from waveflow.hw.synth import synthesizable
from waveflow.simulation.simulation import Simulation


def _make_stream_comp(run_proc_body_cls):
    """Instantiate a component that has s_in and m_out stream endpoints."""
    sim = Simulation()
    comp = run_proc_body_cls(sim=sim)
    return comp


# ---------------------------------------------------------------------------
# Helper base: component with s_in slave + m_out master
# ---------------------------------------------------------------------------

class _BaseStreamComp(HwModule):
    def __post_init__(self):
        super().__post_init__()
        self.s_in = StreamIFSlave(
            name=f'{self.name}_s_in', sim=self.sim, bitwidth=32,
        )
        self.m_out = StreamIFMaster(
            name=f'{self.name}_m_out', sim=self.sim, bitwidth=32,
        )
        self.add_endpoint(self.s_in)
        self.add_endpoint(self.m_out)

    def run_proc(self):  # overridden below
        while True:
            yield from self.s_in.get()

    def on_start(self):  # overridden below
        yield from self.s_in.get()
        return


# ---------------------------------------------------------------------------
# Phase 1 tests: get_pipelined in run_proc raises SynthesisError
# ---------------------------------------------------------------------------

class _GetPipelinedInRunProc(_BaseStreamComp):
    def run_proc(self):
        while True:
            data, tstart = yield from self.s_in.get_pipelined(count=4)


def test_get_pipelined_in_run_proc_raises():
    """get_pipelined inside run_proc (extracted body) must raise SynthesisError."""
    sim = Simulation()
    comp = _GetPipelinedInRunProc(sim=sim)
    with pytest.raises(SynthesisError, match="Pipelined"):
        HwStmtExtractor(comp, method_name='run_proc').extract()


def test_get_pipelined_error_mentions_hook():
    """Error message must direct the user to a hook body."""
    sim = Simulation()
    comp = _GetPipelinedInRunProc(sim=sim)
    with pytest.raises(SynthesisError, match="hook"):
        HwStmtExtractor(comp, method_name='run_proc').extract()


# ---------------------------------------------------------------------------
# write_pipelined in run_proc raises SynthesisError
# ---------------------------------------------------------------------------

class _WritePipelinedInRunProc(_BaseStreamComp):
    def run_proc(self):
        while True:
            yield from self.s_in.get()
            yield from self.m_out.write_pipelined([], 0.0)


def test_write_pipelined_in_run_proc_raises():
    """write_pipelined inside run_proc (extracted body) must raise SynthesisError."""
    sim = Simulation()
    comp = _WritePipelinedInRunProc(sim=sim)
    with pytest.raises(SynthesisError, match="Pipelined"):
        HwStmtExtractor(comp, method_name='run_proc').extract()


# ---------------------------------------------------------------------------
# get_pipelined in on_start raises SynthesisError
# ---------------------------------------------------------------------------

class _GetPipelinedInOnStart(_BaseStreamComp):
    def on_start(self):
        data, tstart = yield from self.s_in.get_pipelined(count=4)
        return


def test_get_pipelined_in_on_start_raises():
    """get_pipelined inside on_start (extracted body) must raise SynthesisError."""
    sim = Simulation()
    comp = _GetPipelinedInOnStart(sim=sim)
    with pytest.raises(SynthesisError, match="Pipelined"):
        HwStmtExtractor(comp, method_name='on_start').extract()


# ---------------------------------------------------------------------------
# Negative: non-pipelined get extracts cleanly
# ---------------------------------------------------------------------------

class _NormalGetInRunProc(_BaseStreamComp):
    def run_proc(self):
        while True:
            x = yield from self.s_in.get()


def test_normal_get_extracts_cleanly():
    """s_in.get() (non-pipelined) must extract without error."""
    sim = Simulation()
    comp = _NormalGetInRunProc(sim=sim)
    tree = HwStmtExtractor(comp, method_name='run_proc').extract()
    assert tree is not None


# ---------------------------------------------------------------------------
# Negative: hook body calling get_pipelined is fine — hooks are NOT extracted
# ---------------------------------------------------------------------------

class _HookWithPipelined(_BaseStreamComp):
    @synthesizable
    def pipelined_evaluate(self, ep):
        data, tstart = yield from ep.get_pipelined(count=4)
        return data

    def run_proc(self):
        while True:
            x = yield from self.s_in.get()
            result = yield from self.pipelined_evaluate(self.s_in)


def test_hook_with_get_pipelined_not_extracted():
    """A hook body that calls get_pipelined is fine — the hook body is not extracted."""
    sim = Simulation()
    comp = _HookWithPipelined(sim=sim)
    # run_proc calls pipelined_evaluate which in turn calls get_pipelined,
    # but the extractor only sees the hook CALL, not the hook BODY.
    # So extraction must succeed (no SynthesisError).
    tree = HwStmtExtractor(comp, method_name='run_proc').extract()
    assert tree is not None
