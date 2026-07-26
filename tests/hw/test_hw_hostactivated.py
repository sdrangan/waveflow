"""Tests for :class:`HostActivated` — the host-activated (regmap-launched) synth leaf."""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from waveflow.build.codegen_dispatch import CodegenPath, codegen_path
from waveflow.build.elaborate import elaborate
from waveflow.hw.dataschema import IntField
from waveflow.hw.hw_module import ControlMode, HwModule
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
    assert issubclass(HostActivated, HwModule)


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
    from examples.regmap.simp_fun import SimpFun
    from examples.stream_inband.poly import PolyAccel
    assert issubclass(SimpFun, HostActivated)
    assert issubclass(PolyAccel, HostActivated)


# ---------------------------------------------------------------------------
# run_once — the one-call invocation.
#
# simp_fun's on_start now *yields* (it models its own latency), so the
# synchronous run_once raises on it — the functional checks below drive
# run_once_sim inside a Simulation instead (see the run_once_sim section).
# run_once itself stays the synchronous path, exercised via a synchronous
# fixture (`_HASync`).
# ---------------------------------------------------------------------------

def test_run_once_computes_relu_affine():
    from examples.regmap.simp_fun import SimpFun
    assert int(_run_once_sim(SimpFun, 5, 3, 2).val) == 17   # relu(3*5+2)
    assert int(_run_once_sim(SimpFun, 2, -5, 3).val) == 0   # relu(-10+3) -> 0


def test_run_once_synchronous_on_start():
    """run_once (synchronous) still works for a kernel whose on_start returns None."""
    dut = elaborate(_HASync)
    assert int(dut.run_once(7).val) == 8


def test_run_once_call_alias():
    """``dut(x)`` is the __call__ alias of the synchronous run_once."""
    dut = elaborate(_HASync)
    assert int(dut(41).val) == 42


def test_run_once_raises_on_yielding_on_start():
    """A yielding on_start needs the sim-driven path; synchronous run_once says so."""
    from examples.regmap.simp_fun import SimpFun
    dut = elaborate(SimpFun)
    with pytest.raises(NotImplementedError, match="run_once_sim"):
        dut.run_once(5, 3, 2)


def test_run_once_wrong_arg_count_raises():
    from examples.regmap.simp_fun import SimpFun
    dut = elaborate(SimpFun)
    with pytest.raises(TypeError, match=r"takes 3 input"):
        dut.run_once(1, 2)


def test_run_once_stream_bearing_is_follow_on():
    """Phase 5a is regmap-scalar only; a stream-bearing kernel (poly) raises a clear follow-on error."""
    from examples.stream_inband.poly import PolyAccel
    dut = elaborate(PolyAccel)
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
class _HASync(HostActivated):
    """A HostActivated whose on_start is synchronous (returns None): y = x + 1."""

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

    def on_start(self):  # synchronous — no yield, returns None
        self.regmap.set("y", _Int32(int(self.regmap.get("x").val) + 1))


def _run_once_sim(comp_class, *args):
    """Build a Simulation, drive ``comp_class.run_once_sim(*args)`` to completion, return result."""
    sim = Simulation()
    dut = comp_class(name="dut", sim=sim)
    drv = _InvokeDriver(name="drv", sim=sim, dut=dut, args=args)
    sim.run_sim()
    return drv.result


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


def test_run_once_sim_matches_golden_for_yielding_on_start():
    """run_once_sim drives simp_fun's yielding on_start and returns the relu-affine golden."""
    from examples.regmap.simp_fun import SimpFun, relu_affine
    for x, a, b in [(5, 3, 2), (0, 7, 1), (2, -5, 3), (-4, -2, 0)]:
        result = _run_once_sim(SimpFun, x, a, b)
        assert int(result.val) == relu_affine(x, a, b), (x, a, b)


def test_run_once_sim_matches_run_once_for_synchronous_on_start():
    """For a synchronous on_start, run_once_sim returns the same value as run_once."""
    for x in [7, 0, -3, 41]:
        expected = int(elaborate(_HASync).run_once(x).val)
        assert int(_run_once_sim(_HASync, x).val) == expected, x


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
    from examples.regmap.simp_fun import SimpFun
    sim = Simulation()
    dut = SimpFun(name="dut", sim=sim)
    with pytest.raises(TypeError, match=r"takes 3 input"):
        # A generator only runs on first advance; drive it to trip the check.
        list(dut.run_once_sim(1, 2))
