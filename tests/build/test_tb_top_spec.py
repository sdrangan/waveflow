"""Tests for `tb_top_spec` — the XSI testbench harness derived from a testbench component graph.

The claim under test: a testbench declared as a composite `FreeRunMod` carries enough information to
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

    Cross-check against the generated harness (examples/mem_copy/xsi/mem_copy_tb_harness.h):

        FlatMemory      mem(...);
        AxisMaster      s_cmd (sim.dut(), ports::s_cmd, {});   // s_cmd.in_bundle = "vectors/s_cmd";
        AxisSlave       s_done(sim.dut(), ports::s_done);
        AxiMmReadSlave  m_in  (sim.dut(), ports::m_in,  mem);
        AxiMmWriteSlave m_out (sim.dut(), ports::m_out, mem);
    """
    spec = tb_top_spec(_tb())
    assert spec.top_name == "mem_copy"

    got = {m.name: (m.cls, m.xsi_prefix, m.args, m.dyn_params) for m in spec.models}
    assert got == {
        # s_cmd is bundle-driven: empty ctor words ({}), and an in_bundle DynParam it loads in pre_sim.
        "s_cmd":  ("AxisMaster", "s_cmd", ("{}",), (("in_bundle", '"vectors/s_cmd"'),)),
        "s_done": ("AxisSlave", "s_done", (), (("out_bundle", '"vectors/s_done"'),)),
        "m_in":   ("AxiMmReadSlave", "m_axi_gmem0", ("mem",), ()),
        "m_out":  ("AxiMmWriteSlave", "m_axi_gmem1", ("mem",), ()),
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
    cls, name, args, _dyn = spec.shared[0]
    assert cls == "FlatMemory" and name == "mem"
    assert args == ("2624", "8"), "arena size/bpw come from the MemModel's own fields"

    slaves = [m for m in spec.models if m.cls.startswith("AxiMm")]
    assert len(slaves) == 2
    assert {m.cls for m in slaves} == {"AxiMmReadSlave", "AxiMmWriteSlave"}
    assert all(m.args == ("mem",) for m in slaves), "both slaves serve the SAME arena"
    assert len({m.xsi_prefix for m in slaves}) == 2, "but each drives its own bundle"


def test_read_vs_write_slave_comes_from_the_boundary_not_the_memory():
    """A memory does not know how it will be driven.

    The kernel is the m_axi MASTER, so the TB must supply the slave — and which KIND is a property of
    the DUT's boundary port (`maxi_read`/`maxi_write`), not of the MemModel. Both bundles resolve
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
    assert {m.name for m in spec.models} == {e[0] for e in tb.dut.boundary}


def test_an_unwired_dut_port_fails_loudly():
    """An unmodelled port would leave the kernel waiting on a wire nobody drives — a hang thousands
    of cycles later with no diagnostic. Refuse at generate time instead."""
    tb = _tb()
    from waveflow.hw.interface import StreamIFSlave
    ghost = StreamIFSlave(name="s_ghost", sim=tb.sim, bitwidth=64, has_tlast=False)
    tb.dut.boundary = tuple(tb.dut.boundary) + (("s_ghost", ghost, None),)
    with pytest.raises(ValueError, match="not wired to any testbench participant"):
        tb_top_spec(tb)


def test_the_dut_is_found_by_its_boundary_not_by_kernel_task():
    """Regression: `kernel_task` does NOT identify the DUT — a composite has none (only its
    children do), so both the DUT and the participants answer False. The discriminator is
    `boundary` (RTL ports) vs `bfm_model()` (a TB model)."""
    tb = _tb()
    assert not hasattr(tb.dut, "kernel_task"), "if this ever gains one, re-check _find_dut"
    assert hasattr(tb.dut, "boundary") and not hasattr(tb.dut, "bfm_model")
    assert hasattr(tb.driver, "bfm_model") and not hasattr(tb.driver, "boundary")
