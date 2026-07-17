"""Tests for the elaboration contract (Phase 0).

Covers the sim-free :class:`ElabContext`, the single :func:`elaborate` entry,
the name/identity-agnostic :func:`structure_signature`, and the param-purity
determinism gate (:func:`assert_param_pure` / :class:`ParamPurityError`).
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass
from typing import ClassVar

import pytest

from waveflow.build.elaborate import (
    ElabContext,
    ParamPurityError,
    assert_param_pure,
    elaborate,
    structure_signature,
)
from waveflow.hw.hw_component import HwParam
from waveflow.hw.interface import StreamIFMaster
from waveflow.hw.mem_stream import FreeRunComp, MemRStream, MemWStream


# ---------------------------------------------------------------------------
# ElabContext — the sim-free stand-in
# ---------------------------------------------------------------------------

class TestElabContext:
    def test_is_a_simulation_subclass(self):
        from waveflow.simulation.simulation import Simulation
        assert issubclass(ElabContext, Simulation)
        assert ElabContext().is_elaboration is True

    def test_env_is_lazy(self):
        ctx = ElabContext()
        assert ctx._env is None            # nothing allocated up front
        env = ctx.env                       # created on first touch
        assert env is not None
        assert ctx.env is env               # and cached

    def test_add_obj_collects_without_running(self):
        ctx = ElabContext()
        sentinel = object()
        ctx.add_obj(sentinel)
        assert sentinel in ctx._sim_objs

    def test_run_sim_refuses(self):
        with pytest.raises(RuntimeError, match="must not be run"):
            ElabContext().run_sim()


# ---------------------------------------------------------------------------
# elaborate() — the single construction entry
# ---------------------------------------------------------------------------

class TestElaborate:
    def test_builds_structure_and_applies_params(self):
        comp = elaborate(MemRStream)
        assert set(comp.endpoints) == {
            "_codegen_m_mem", "_codegen_s_cmd", "_codegen_m_out"
        }

    def test_param_override_changes_structure(self):
        # emit_done adds the s_done endpoint (structure is param-driven).
        plain = elaborate(MemWStream)
        done = elaborate(MemWStream, {"emit_done": True})
        assert not any(ep.endswith("_s_done") for ep in plain.endpoints)
        assert any(ep.endswith("_s_done") for ep in done.endpoints)

    def test_uses_an_elaboration_context(self):
        comp = elaborate(MemRStream)
        assert isinstance(comp.sim, ElabContext)


# ---------------------------------------------------------------------------
# structure_signature — name / identity agnostic
# ---------------------------------------------------------------------------

class TestStructureSignature:
    def test_ignores_names_and_identity(self):
        a = elaborate(MemRStream, name="foo", check_purity=False)
        b = elaborate(MemRStream, name="bar_totally_different", check_purity=False)
        assert structure_signature(a) == structure_signature(b)

    def test_distinguishes_real_structural_difference(self):
        plain = elaborate(MemWStream, check_purity=False)
        done = elaborate(MemWStream, {"emit_done": True}, check_purity=False)
        assert structure_signature(plain) != structure_signature(done)


# ---------------------------------------------------------------------------
# assert_param_pure — the determinism gate
# ---------------------------------------------------------------------------

# Canonical Phase-1 components (leaf + composite) that must be param-pure.
def _pure_cases():
    from examples.interleaver.interleaver import InterleaverCanon
    from examples.mem_copy.mem_copy import MemCopy
    return [
        (MemRStream, None),
        (MemWStream, None),
        (MemWStream, {"emit_done": True}),
        (MemCopy, {"mem_dwidth": 64}),
        (InterleaverCanon, {"mem_dwidth": 64, "n": 256}),
    ]


@pytest.mark.parametrize("cls,params", _pure_cases())
def test_known_components_are_param_pure(cls, params):
    assert_param_pure(cls, params)


def test_impurity_is_caught():
    """Structure driven by a global counter (not params) must fail loudly."""
    ctr = itertools.count()

    @dataclass
    class _Impure(FreeRunComp):
        cpp_kernel_name: ClassVar[str] = "_impure"
        width: HwParam[int] = 32

        def __post_init__(self):
            super().__post_init__()
            n = next(ctr) % 2 + 1          # 1, then 2, then 1, ...  -> impure
            for i in range(n):
                self.add_endpoint(StreamIFMaster(
                    name=f"{self.name}_out{i}", sim=self.sim,
                    bitwidth=int(self.width)))

        def run_iter(self):
            yield self.timeout(0)

    with pytest.raises(ParamPurityError, match="not a pure function"):
        assert_param_pure(_Impure)
