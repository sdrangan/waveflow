"""Tests for :class:`HostActivated` — the host-activated (regmap-launched) synth leaf."""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from waveflow.build.codegen_dispatch import CodegenPath, codegen_path
from waveflow.build.elaborate import elaborate
from waveflow.hw.dataschema import IntField
from waveflow.hw.hw_component import ControlMode, SynthComp
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


def test_hostactivated_is_a_synthcomp():
    assert issubclass(HostActivated, SynthComp)


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


def test_hostactivated_requires_on_start_implemented():
    """SynthComp._check_synthesizable verifies the declared entry (on_start) exists at construction."""
    @dataclass
    class _NoOnStart(HostActivated):
        pass

    with pytest.raises(TypeError, match="on_start"):
        elaborate(_NoOnStart)


def test_migrated_examples_are_hostactivated():
    from examples.regmap.simp_fun import SimpFunComponent
    from examples.stream_inband.poly import PolyAccelComponent
    assert issubclass(SimpFunComponent, HostActivated)
    assert issubclass(PolyAccelComponent, HostActivated)
