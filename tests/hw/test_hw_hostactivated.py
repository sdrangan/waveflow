"""Tests for :class:`HostActivated` — the host-activated (regmap-launched) synth leaf."""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from waveflow.build.codegen_dispatch import CodegenPath, codegen_path
from waveflow.build.elaborate import elaborate
from waveflow.hw.dataschema import IntField
from waveflow.hw.hw_component import ControlMode, HwComponent
from waveflow.hw.hw_hostactivated import HostActivated
from waveflow.hw.regmap import RegAccess, RegField, VitisRegMap, VitisRegMapMMIFSlave
from waveflow.simulation.simobj import ProcessGen

_Int32 = IntField.specialize(bitwidth=32, signed=True)


@dataclass
class _HA(HostActivated):
    def __post_init__(self):
        super().__post_init__()
        self.regmap = VitisRegMap({"y": RegField(_Int32, RegAccess.R)})
        self.s_lite = VitisRegMapMMIFSlave(
            name=f"{self.name}_s_lite", sim=self.sim, bitwidth=32,
            regmap=self.regmap, on_start=self.on_start)
        self.add_endpoint(self.s_lite)

    def on_start(self) -> ProcessGen[None]:
        yield


def test_hostactivated_is_a_hwcomponent():
    assert issubclass(HostActivated, HwComponent)


def test_hostactivated_declares_on_start_entry():
    assert HostActivated._kernel_method == "on_start"


def test_hostactivated_control_mode_per_invocation():
    assert HostActivated.control_mode == ControlMode.PER_INVOCATION


def test_hostactivated_dispatches_to_on_start():
    assert codegen_path(elaborate(_HA)) == CodegenPath("leaf", "on_start")


def test_hostactivated_rejects_run_iter():
    """A host-activated leaf runs once per trigger — defining run_iter fails at class definition."""
    with pytest.raises(TypeError, match="defines run_iter"):
        @dataclass
        class _Bad(HostActivated):
            def run_iter(self):  # noqa: ANN201
                yield


def test_migrated_examples_are_hostactivated():
    from examples.regmap.simp_fun import SimpFunComponent
    from examples.stream_inband.poly import PolyAccelComponent
    assert issubclass(SimpFunComponent, HostActivated)
    assert issubclass(PolyAccelComponent, HostActivated)


# ---------------------------------------------------------------------------
# run_once — the one-call invocation (Phase 5a)
# ---------------------------------------------------------------------------

def _explicit_simp_fun(dut, x, a, b):
    """The explicit regmap.set / on_start / regmap.get sequence run_once replaces."""
    dut.regmap.set("x", x)
    dut.regmap.set("a", a)
    dut.regmap.set("b", b)
    dut.on_start()
    return dut.regmap.get("y")


def test_run_once_matches_explicit_sequence():
    from examples.regmap.simp_fun import SimpFunComponent
    for x, a, b in [(5, 3, 2), (0, 7, 1), (2, -5, 3), (-4, -2, 0)]:
        once = elaborate(SimpFunComponent).run_once(x, a, b)
        expl = _explicit_simp_fun(elaborate(SimpFunComponent), x, a, b)
        assert int(once.val) == int(expl.val), (x, a, b)


def test_run_once_computes_relu_affine():
    from examples.regmap.simp_fun import SimpFunComponent
    assert int(elaborate(SimpFunComponent).run_once(5, 3, 2).val) == 17   # relu(3*5+2)
    assert int(elaborate(SimpFunComponent).run_once(2, -5, 3).val) == 0   # relu(-10+3) -> 0


def test_run_once_call_alias():
    from examples.regmap.simp_fun import SimpFunComponent
    dut = elaborate(SimpFunComponent)
    assert int(dut(5, 3, 2).val) == 17


def test_run_once_wrong_arg_count_raises():
    from examples.regmap.simp_fun import SimpFunComponent
    dut = elaborate(SimpFunComponent)
    with pytest.raises(TypeError, match=r"takes 3 input"):
        dut.run_once(1, 2)


def test_run_once_stream_bearing_is_follow_on():
    """Phase 5a is regmap-scalar only; a stream-bearing kernel (poly) raises a clear follow-on error."""
    from examples.stream_inband.poly import PolyAccelComponent
    dut = elaborate(PolyAccelComponent)
    with pytest.raises(NotImplementedError, match="stream-bearing"):
        dut.run_once(1)
