from __future__ import annotations

import typing
from dataclasses import dataclass
from typing import ClassVar

import pytest

from waveflow.hw.hw_module import (
    ControlMode,
    HwModule,
    HwParam,
    SynthContext,
)
from waveflow.hw.synth import synthesizable
from waveflow.simulation.simulation import Simulation


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sentinel_synth_fn(ctx, inputs, outputs):
    return ""


@dataclass
class ParamComp(HwModule):
    in_bw: HwParam[int] = 32
    out_bw: HwParam[int] = 64
    max_taps: ClassVar[int] = 16


# ---------------------------------------------------------------------------
# @synthesizable decorator
# ---------------------------------------------------------------------------

def test_synthesizable_no_args_sets_flag():
    @synthesizable
    def my_fn(self):
        pass

    assert my_fn._is_synthesizable is True
    assert my_fn._synth_fn is None


def test_synthesizable_with_synth_fn():
    @synthesizable(synth_fn=_sentinel_synth_fn)
    def my_fn(self):
        pass

    assert my_fn._is_synthesizable is True
    assert my_fn._synth_fn is _sentinel_synth_fn


def test_synthesizable_parens_no_fn_sets_flag():
    @synthesizable()
    def my_fn(self):
        pass

    assert my_fn._is_synthesizable is True
    assert my_fn._synth_fn is None


def test_synthesizable_wraps_original_function():
    @synthesizable
    def compute(self, x):
        return x + 1

    assert compute.__name__ == "compute"


# ---------------------------------------------------------------------------
# HwParam detection
# ---------------------------------------------------------------------------


def test_hwparam_detectable_via_get_origin():
    hint = HwParam[int]
    assert typing.get_origin(hint) is HwParam


def test_hwparam_detectable_for_nested_types():
    hint = HwParam[list[int]]
    assert typing.get_origin(hint) is HwParam


# ---------------------------------------------------------------------------
# SynthContext.from_component
# ---------------------------------------------------------------------------

def test_synth_context_extracts_hwparam_fields():
    sim = Simulation()
    comp = ParamComp(sim=sim)
    ctx = SynthContext.from_component(comp)

    assert 'in_bw' in ctx.params
    assert 'out_bw' in ctx.params
    assert ctx.params['in_bw'] == 'IN_BW'
    assert ctx.params['out_bw'] == 'OUT_BW'


def test_synth_context_excludes_classvar():
    sim = Simulation()
    comp = ParamComp(sim=sim)
    ctx = SynthContext.from_component(comp)

    assert 'max_taps' not in ctx.params


def test_synth_context_excludes_plain_fields():
    sim = Simulation()
    comp = ParamComp(sim=sim)
    ctx = SynthContext.from_component(comp)

    # 'name', 'sim', 'endpoints' are plain inherited fields — not HwParam
    assert 'name' not in ctx.params
    assert 'sim' not in ctx.params
    assert 'endpoints' not in ctx.params


# ---------------------------------------------------------------------------
# SynthContext.cpp_param
# ---------------------------------------------------------------------------

def test_cpp_param_returns_template_name_for_hwparam():
    sim = Simulation()
    comp = ParamComp(sim=sim)
    ctx = SynthContext.from_component(comp)

    assert ctx.cpp_param('in_bw') == 'IN_BW'
    assert ctx.cpp_param('out_bw') == 'OUT_BW'


def test_cpp_param_returns_repr_for_classvar():
    sim = Simulation()
    comp = ParamComp(sim=sim)
    ctx = SynthContext.from_component(comp)

    assert ctx.cpp_param('max_taps') == '16'


def test_cpp_param_custom_value():
    sim = Simulation()
    comp = ParamComp(sim=sim, in_bw=128)
    ctx = SynthContext.from_component(comp)

    # HwParam field → template name regardless of runtime value
    assert ctx.cpp_param('in_bw') == 'IN_BW'


# ---------------------------------------------------------------------------
# HwModule instantiation
# ---------------------------------------------------------------------------

def test_hwcomponent_is_a_component():
    from waveflow.simulation.simobj import SimObj
    sim = Simulation()
    comp = HwModule(sim=sim)
    assert isinstance(comp, SimObj)


def test_hwcomponent_default_control_mode():
    assert HwModule.control_mode == ControlMode.AUTO


def test_hwcomponent_control_mode_override():
    class FreeRunMod(HwModule):
        control_mode: ClassVar[ControlMode] = ControlMode.FREE_RUNNING

    assert FreeRunMod.control_mode == ControlMode.FREE_RUNNING


def test_hwcomponent_subclass_with_hwparam_instantiates():
    sim = Simulation()
    comp = ParamComp(sim=sim, in_bw=16, out_bw=32)
    assert comp.in_bw == 16
    assert comp.out_bw == 32


# ---------------------------------------------------------------------------
# Phase 2: HwParamValue auto-wrap
# ---------------------------------------------------------------------------

def test_hwparam_value_wrapped_after_construction():
    from waveflow.hw.hw_module import HwParamValue
    comp = ParamComp(sim=Simulation(), in_bw=32, out_bw=64)
    assert isinstance(comp.in_bw, HwParamValue)
    assert comp.in_bw.param_name == 'in_bw'
    assert int(comp.in_bw) == 32
    assert isinstance(comp.out_bw, HwParamValue)
    assert comp.out_bw.param_name == 'out_bw'


def test_hwparam_value_behaves_as_int():
    comp = ParamComp(sim=Simulation(), in_bw=32, out_bw=64)
    assert comp.in_bw + 1 == 33
    assert comp.in_bw == 32
    assert comp.in_bw * 2 == 64
    assert int(comp.in_bw) == 32


def test_hwparam_value_equals_int_literal():
    from waveflow.hw.hw_module import HwParamValue
    assert HwParamValue(32, 'in_bw') == 32


def test_hwparam_value_formats_as_int():
    from waveflow.hw.hw_module import HwParamValue
    bw = HwParamValue(32, 'in_bw')
    assert f"<{bw}>" == "<32>"
    assert str(bw) == "32"


def test_plain_field_not_wrapped():
    from dataclasses import dataclass

    @dataclass
    class _PlainFieldComp(HwModule):
        in_bw: HwParam[int] = 32
        proc_latency: int = 10

    comp = _PlainFieldComp(sim=Simulation())
    assert not hasattr(comp.proc_latency, 'param_name')
    assert type(comp.proc_latency) is int


# ---------------------------------------------------------------------------
# Phase 3: HwParam immutability
# ---------------------------------------------------------------------------

def test_hwparam_reassignment_raises_attribute_error():
    comp = ParamComp(sim=Simulation(), in_bw=32, out_bw=64)
    with pytest.raises(AttributeError, match="in_bw"):
        comp.in_bw = 64


def test_hwparam_reassign_error_mentions_current_value():
    comp = ParamComp(sim=Simulation(), in_bw=32, out_bw=64)
    with pytest.raises(AttributeError, match="32"):
        comp.in_bw = 64


def test_plain_field_remains_mutable_after_construction():
    from dataclasses import dataclass

    @dataclass
    class _PlainMutableComp(HwModule):
        in_bw: HwParam[int] = 32
        proc_ii: int = 1

    comp = _PlainMutableComp(sim=Simulation())
    comp.proc_ii = 2
    assert comp.proc_ii == 2


def test_internal_state_attribute_remains_mutable():
    """Names without an annotation (e.g. counters) can be reassigned freely."""
    comp = ParamComp(sim=Simulation(), in_bw=32, out_bw=64)
    comp._job = 42  # noqa: SLF001 — exercising the mutability path
    assert comp._job == 42


def test_construction_still_works():
    """Sanity: instantiation isn't blocked by the immutability guard."""
    comp = ParamComp(sim=Simulation(), in_bw=16, out_bw=32)
    assert int(comp.in_bw) == 16


def test_subclass_post_init_sees_wrapped_param():
    """Endpoints constructed in subclass __post_init__ must read HwParamValue."""
    from dataclasses import dataclass
    from waveflow.hw.hw_module import HwParamValue
    from waveflow.hw.interface import StreamIFSlave

    @dataclass
    class _StreamComp(HwModule):
        in_bw: HwParam[int] = 32

        def __post_init__(self) -> None:
            super().__post_init__()
            self.s_in = StreamIFSlave(
                name=f'{self.name}_s_in', sim=self.sim, bitwidth=self.in_bw,
            )

    comp = _StreamComp(name="c", sim=Simulation(), in_bw=64)
    assert isinstance(comp.s_in.bitwidth, HwParamValue)
    assert comp.s_in.bitwidth.param_name == 'in_bw'
    assert int(comp.s_in.bitwidth) == 64
