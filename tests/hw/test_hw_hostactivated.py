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


# ---------------------------------------------------------------------------
# run_once_sim — the sim-driven invocation (drives on_start through SimPy)
# ---------------------------------------------------------------------------

from dataclasses import field  # noqa: E402
from typing import Any  # noqa: E402

from waveflow.hw.clock import Clock  # noqa: E402
from waveflow.simulation.simobj import SimObj  # noqa: E402
from waveflow.simulation.simulation import Simulation  # noqa: E402


@dataclass(kw_only=True)
class _InvokeDriver(SimObj):
    """Tiny process that drives ``dut.run_once_sim(*args)`` and records result + timing."""
    dut: Any
    args: tuple
    result: Any = None
    t0: float = 0.0
    t1: float = 0.0

    def run_proc(self) -> ProcessGen[None]:
        self.t0 = self.now
        self.result = yield from self.dut.run_once_sim(*self.args)
        self.t1 = self.now


@dataclass
class _HAYield(HostActivated):
    """A HostActivated whose on_start *yields* a timeout (a latency model) then copies x -> y."""
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))
    delay_cycles: int = 5

    def __post_init__(self):
        super().__post_init__()
        self.regmap = VitisRegMap({
            "x": RegField(_Int32, RegAccess.RW),
            "y": RegField(_Int32, RegAccess.R),
        })
        self.regmap.set("y", 0)
        self.s_lite = VitisRegMapMMIFSlave(
            name=f"{self.name}_s_lite", sim=self.sim, bitwidth=32,
            regmap=self.regmap, on_start=self.on_start)
        self.add_endpoint(self.s_lite)

    def on_start(self) -> ProcessGen[None]:
        yield self.timeout(self.delay_cycles * self.clk.period)
        self.regmap.set("y", self.regmap.get("x"))


def test_run_once_sim_matches_run_once_for_synchronous_on_start():
    """With simp_fun's synchronous on_start, run_once_sim returns the same value as run_once."""
    from examples.regmap.simp_fun import SimpFunComponent
    for x, a, b in [(5, 3, 2), (0, 7, 1), (2, -5, 3), (-4, -2, 0)]:
        expected = int(elaborate(SimpFunComponent).run_once(x, a, b).val)

        sim = Simulation()
        dut = SimpFunComponent(name="dut", sim=sim)
        drv = _InvokeDriver(name="drv", sim=sim, dut=dut, args=(x, a, b))
        sim.run_sim()
        assert int(drv.result.val) == expected, (x, a, b)


def test_run_once_sim_advances_clock_for_yielding_on_start():
    """A yielding on_start (yield self.timeout(...)) advances the sim clock; t1 - t0 > 0."""
    sim = Simulation()
    clk = Clock(freq=100e6)
    dut = _HAYield(name="dut", sim=sim, clk=clk, delay_cycles=5)
    drv = _InvokeDriver(name="drv", sim=sim, dut=dut, args=(42,))
    sim.run_sim()
    assert drv.t1 - drv.t0 > 0
    assert drv.t1 - drv.t0 == 5 * clk.period
    assert int(drv.result.val) == 42


def test_run_once_sim_wrong_arg_count_raises():
    from examples.regmap.simp_fun import SimpFunComponent
    sim = Simulation()
    dut = SimpFunComponent(name="dut", sim=sim)
    with pytest.raises(TypeError, match=r"takes 3 input"):
        # A generator only runs on first advance; drive it to trip the check.
        list(dut.run_once_sim(1, 2))
