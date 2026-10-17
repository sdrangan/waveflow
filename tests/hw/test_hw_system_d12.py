"""``HwSystem(sim, dut)`` -- a simulation plus one named DUT (``plans/unit_harness.md`` D12), and the
harness built that way (D2, D11).  Each rule is a refusal that names what to fix; the testbench is
derived, never listed."""
from __future__ import annotations

import pytest

from examples.stream_kernel.stream_kernel import PolyKernel
from waveflow.hw.harness import harness
from waveflow.hw.hw_module import HwModule
from waveflow.hw.hw_system import DutTop, HwSystem
from waveflow.simulation.simulation import Simulation


def test_the_dut_must_be_in_the_simulation():
    other = Simulation()
    k = PolyKernel(name="poly_kernel", sim=other)
    with pytest.raises(ValueError, match="not a SimObj of this simulation"):
        HwSystem(Simulation(), k)


def test_the_dut_must_be_a_root():
    sim = Simulation()
    k = PolyKernel(name="poly_kernel", sim=sim)
    DutTop.of("top", sim, comps=[k])
    with pytest.raises(ValueError, match="a system's DUT is a root"):
        HwSystem(sim, k)


def test_no_boundary_port_may_be_left_unbound():
    sim = Simulation()
    k = PolyKernel(name="poly_kernel", sim=sim)
    with pytest.raises(ValueError, match="bound to nothing: .*s_in"):
        HwSystem(sim, k)


def test_every_participant_must_lower():
    """An object outside the DUT with no bfm_model / xsi_model / twin belongs in a plain Simulation."""
    sysm = harness(PolyKernel)
    HwModule(name="stray", sim=sysm.sim)
    with pytest.raises(ValueError, match="no testbench lowering.*stray"):
        HwSystem(sysm.sim, sysm.dut())


def test_a_dut_with_nothing_in_the_cut_is_refused():
    """An RTL run of it would be vacuous -- it would 'pass' with no design in it."""
    sim = Simulation()
    top = DutTop.of("empty", sim)
    sysm = HwSystem(sim, top)
    with pytest.raises(ValueError, match="puts nothing in the cut"):
        sysm.cut()


def test_the_testbench_is_derived_and_a_kernel_is_its_own_cut():
    sysm = harness(PolyKernel)
    assert [m.name for m in sysm.cut()] == ["poly_kernel"]
    names = sorted(p.name for p in sysm.participants())
    assert names == ["m_out_sink", "m_status_sink", "s_in_driver"]


def test_a_harness_copies_a_module_out_of_its_system():
    """A module inside a larger system is harnessed as a fresh copy: the original keeps its parent and
    its simulation."""
    sim = Simulation()
    k = PolyKernel(name="poly_kernel", sim=sim)
    top = DutTop.of("top", sim, comps=[k])
    sysm = harness(k)
    assert sysm.dut() is not k and sysm.sim is not sim
    assert k.parent is top
    assert type(sysm.dut()) is PolyKernel


def test_fresh_is_an_unrun_copy():
    sysm = harness(PolyKernel)
    again = sysm.fresh()
    assert again is not sysm and again.sim is not sysm.sim
    assert type(again.dut()) is type(sysm.dut())
