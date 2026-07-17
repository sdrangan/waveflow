"""Tests for `tb_top_spec` — the XSI testbench harness derived from a testbench component graph.

The claim under test: a testbench declared as a `CompositeComp` carries enough information to
*derive* the models the hand-written testbench constructs by hand. If this holds, `main()` is
emittable; if it does not, Stage 5 is dead.

These are toolchain-free (pure graph resolution), so they run in the fast loop.
"""
from __future__ import annotations

import pytest

from waveflow.build.composite_gen import tb_top_spec
from waveflow.simulation.simulation import Simulation


def _tb(**kw):
    from examples.mem_copy.mem_copy_sim import MemCopyTB
    return MemCopyTB(name="tb", sim=Simulation(), mem_dwidth=64, **kw)


def test_walk_derives_exactly_the_hand_written_testbench():
    """Every model the hand-written mem_copy TB constructs, derived from the graph instead.

    Cross-check against examples/mem_copy/xsi/mem_copy_bfm_tb.cpp:

        FlatMemory      mem(vec::MEM_NW, BPW);
        AxisMaster      s_cmd (sim.dut(), ports::s_cmd,  cmd_words);
        AxisSlave       s_done(sim.dut(), ports::s_done);
        AxiMmReadSlave  gmem0 (sim.dut(), ports::m_in,  mem);
        AxiMmWriteSlave gmem1 (sim.dut(), ports::m_out, mem);
    """
    spec = tb_top_spec(_tb())
    assert spec.top_name == "mem_copy"

    got = {m.name: (m.cls, m.xsi_prefix, m.args) for m in spec.models}
    assert got == {
        "s_cmd":  ("AxisMaster", "s_cmd", ("cmd_words",)),
        "s_done": ("AxisSlave", "s_done", ()),
        "m_in":   ("AxiMmReadSlave", "m_axi_gmem0", ("mem",)),
        "m_out":  ("AxiMmWriteSlave", "m_axi_gmem1", ("mem",)),
    }


def test_one_crossbar_becomes_two_slaves_sharing_one_arena():
    """The case that decides edge-owned lowering.

    In pysim the crossbar is ONE interface with three endpoints (2 masters -> 1 slave). At RTL there
    is no crossbar: each bundle needs its own slave, and both serve the same memory. So one graph
    edge produces two models plus a shared third — which is why the arena is `shared` rather than
    instantiated per bundle. Had the slave models been participants instead, the pysim graph and the
    XSI graph would need different nodes and "one statement, two backends" would break here.
    """
    spec = tb_top_spec(_tb())

    assert len(spec.shared) == 1
    cls, name, args = spec.shared[0]
    assert cls == "FlatMemory" and name == "mem"
    assert args == ("2624", "8"), "arena size/bpw come from the MemComponent's own fields"

    slaves = [m for m in spec.models if m.cls.startswith("AxiMm")]
    assert len(slaves) == 2
    assert {m.cls for m in slaves} == {"AxiMmReadSlave", "AxiMmWriteSlave"}
    assert all(m.args == ("mem",) for m in slaves), "both slaves serve the SAME arena"
    assert len({m.xsi_prefix for m in slaves}) == 2, "but each drives its own bundle"


def test_read_vs_write_slave_comes_from_the_boundary_not_the_memory():
    """A memory does not know how it will be driven.

    The kernel is the m_axi MASTER, so the TB must supply the slave — and which KIND is a property of
    the DUT's boundary port (`maxi_read`/`maxi_write`), not of the MemComponent. Both bundles resolve
    to the same participant; only the boundary kind distinguishes them.
    """
    spec = tb_top_spec(_tb())
    by_prefix = {m.xsi_prefix: m.cls for m in spec.models}
    assert by_prefix["m_axi_gmem0"] == "AxiMmReadSlave"    # boundary kind maxi_read
    assert by_prefix["m_axi_gmem1"] == "AxiMmWriteSlave"   # boundary kind maxi_write


def test_the_boundary_is_the_spine_every_rtl_port_gets_a_model():
    """Coverage is structural, not a review question: the walk iterates the DUT's boundary, so an
    unmodelled port is impossible rather than merely unlikely."""
    tb = _tb()
    spec = tb_top_spec(tb)
    assert {m.name for m in spec.models} == {n for n, _ep, _k, _b in tb.dut.boundary}


def test_an_unwired_dut_port_fails_loudly():
    """An unmodelled port would leave the kernel waiting on a wire nobody drives — a hang thousands
    of cycles later with no diagnostic. Refuse at generate time instead."""
    tb = _tb()
    tb.dut.boundary = tuple(tb.dut.boundary) + (("s_ghost", object(), "axis_in", None),)
    with pytest.raises(ValueError, match="not wired to any testbench participant"):
        tb_top_spec(tb)


def test_the_dut_is_found_by_its_boundary_not_by_kernel_task():
    """Regression: `kernel_task` does NOT identify the DUT — a CompositeComp has none (only its
    children do), so both the DUT and the participants answer False. The discriminator is
    `boundary` (RTL ports) vs `bfm_model()` (a TB model)."""
    tb = _tb()
    assert not hasattr(tb.dut, "kernel_task"), "if this ever gains one, re-check _find_dut"
    assert hasattr(tb.dut, "boundary") and not hasattr(tb.dut, "bfm_model")
    assert hasattr(tb.driver, "bfm_model") and not hasattr(tb.driver, "boundary")
