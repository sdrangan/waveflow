"""Tests for `tb_top_spec` — the XSI testbench harness derived from a testbench component graph.

The claim under test: a testbench declared as a composite `FreeRunMod` carries enough information to
*derive* the models the hand-written testbench constructs by hand. If this holds, `main()` is
emittable; if it does not, Stage 5 is dead.

These are toolchain-free (pure graph resolution), so they run in the fast loop.
"""
from __future__ import annotations

import pytest

from waveflow.build.composite_gen import tb_top_spec
from waveflow.hw.hw_module import declares_hook
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
    assert args == ("2624", "8"), "arena size/bpw come from the MemoryMod's own fields"

    slaves = [m for m in spec.models if m.cls.startswith("AxiMm")]
    assert len(slaves) == 2
    assert {m.cls for m in slaves} == {"AxiMmReadSlave", "AxiMmWriteSlave"}
    assert all(m.args == ("mem",) for m in slaves), "both slaves serve the SAME arena"
    assert len({m.xsi_prefix for m in slaves}) == 2, "but each drives its own bundle"


def test_read_vs_write_slave_comes_from_the_boundary_not_the_memory():
    """A memory does not know how it will be driven.

    The kernel is the m_axi MASTER, so the TB must supply the slave — and which KIND is a property of
    the DUT's boundary port (`maxi_read`/`maxi_write`), not of the MemoryMod. Both bundles resolve
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


# ==============================================================================================
# The protocol x role BFM registry (design_cut S2)
#
# `_SLAVE_FOR_KIND` was two entries, and its AXIS rows were *implicit* — they lived in whatever class
# each participant declared.  So "which duals exist?" had no single answer, and a kind with no dual
# surfaced as a KeyError on a kind string rather than as a named gap.
# ==============================================================================================

def _tb_graphs():
    """Every testbench declared as a graph — the designs `tb_top_spec` actually walks.

    The two standalone mem-stream tops (gates 158 / 176) are deliberately absent: they hand-assemble
    their `main`, so there is no graph to derive and nothing here to reproduce.
    """
    from examples.fir_block.fir_block_sim import FirBlockTB
    from examples.interleaver.interleaver_inband_sim import InterleaverInbandTB
    from examples.mem_copy.mem_copy_sim import MemCopyTB
    from examples.state_toy.state_toy import StateAccumTB

    return [MemCopyTB, InterleaverInbandTB, FirBlockTB, StateAccumTB]


@pytest.mark.parametrize("tb_cls", _tb_graphs(), ids=lambda c: c.__name__)
def test_the_registry_reproduces_todays_model_selection(tb_cls):
    """S2's gate: every graph-declared design resolves to exactly the models it resolved before.

    This is a re-siting exercise over code that works, so the only acceptable outcome is *no change*.
    The assertion is against the **committed generated harness** where one exists — the artifact that
    was actually compiled and run through RTL — rather than against a restatement of the table, which
    would only prove the table equals itself.
    """
    import re
    from pathlib import Path

    tb = tb_cls(name="tb", sim=Simulation())
    spec = tb_top_spec(tb)

    root = Path(__file__).resolve().parents[2] / "examples"
    harness = next(root.glob(f"*/xsi/{spec.top_name}_tb_harness.h"), None)
    if harness is None:
        pytest.skip(f"no committed harness for {spec.top_name} to compare against")

    # The harness declares its participants as `    <Cls> <name>;` inside `struct Harness`.
    text = harness.read_text(encoding="utf-8")
    declared = {name: cls
                for cls, name in re.findall(r"^    ([A-Z]\w+) (\w+);$", text, re.M)
                if cls != "XsiSim"}      # the simulator itself is not a participant model
    got = {m.name: m.cls for m in spec.models}
    got.update({name: cls for cls, name, *_ in spec.shared})
    assert got == declared, "the registry changed which model serves a port"


def test_an_unregistered_kind_names_the_protocol_and_the_role():
    """A kind with no dual must say what is missing, not raise a KeyError on a string.

    Two distinct answers, and the difference matters to whoever reads it: an *unregistered* kind is a
    gap in the table (someone must add a row), while a **registered row with no model** is a known,
    named hole in the model library.
    """
    from waveflow.build.composite_gen import BFM_DUALS, bfm_dual_class
    from waveflow.build.hwcodegen import LoweringError

    with pytest.raises(LoweringError, match="no BFM dual is registered"):
        bfm_dual_class("i2c_target", None)

    # AXI-Lite is the known hole: the protocol and the role are real, the model is not.
    assert BFM_DUALS["axilite_slave"].model is None
    with pytest.raises(LoweringError) as e:
        bfm_dual_class("axilite_slave", None)
    assert "AXI4-Lite" in str(e.value) and "master" in str(e.value)


def test_the_participant_chooses_only_where_the_protocol_leaves_a_choice():
    """The table's one asymmetry, stated as a test rather than left to the reader.

    On AXI-Stream the role fixes the direction but not the class — a source, a sink and a peer that
    never backpressures are three classes in one role.  On m_axi there is nothing to choose: the DUT
    is the master, so the TB supplies the slave the DUT's port kind implies, and a memory does not get
    to decide whether it is read or written.
    """
    from waveflow.build.composite_gen import bfm_dual_class

    assert bfm_dual_class("axis_in", "NeverBackpressureMaster") == "NeverBackpressureMaster"
    assert bfm_dual_class("axis_in", None) == "AxisMaster"          # the reference model
    assert bfm_dual_class("maxi_read", "FlatMemory") == "AxiMmReadSlave"    # declaration ignored
    assert bfm_dual_class("maxi_write", "FlatMemory") == "AxiMmWriteSlave"


def test_the_registry_covers_every_kind_the_endpoint_vocabulary_produces():
    """The table and `kind_of_endpoint` must not drift: a kind with no row is a KeyError waiting."""
    from waveflow.build.composite_gen import BFM_DUALS, kind_of_endpoint
    from waveflow.hw.interface import StreamIFMaster, StreamIFSlave
    from waveflow.hw.memif import MMIFReadMaster, MMIFWriteMaster
    from waveflow.hw.regmap import RegAccess, RegField, VitisRegMap, VitisRegMapMMIFSlave
    from waveflow.hw.dataschema import IntField

    sim = Simulation()
    Int32 = IntField.specialize(bitwidth=32, signed=True)
    eps = [
        StreamIFSlave(name="a", sim=sim, bitwidth=32),
        StreamIFMaster(name="b", sim=sim, bitwidth=32),
        MMIFReadMaster(name="c", sim=sim, bitwidth=64),
        MMIFWriteMaster(name="d", sim=sim, bitwidth=64),
        VitisRegMapMMIFSlave(name="e", sim=sim, bitwidth=32,
                             regmap=VitisRegMap({"x": RegField(Int32, RegAccess.RW)}),
                             on_start=lambda: None),
    ]
    for ep in eps:
        assert kind_of_endpoint(ep) in BFM_DUALS, f"{type(ep).__name__} has no BFM_DUALS row"


# ==============================================================================================
# The cut is an argument (design_cut S4)
#
# `_find_dut` probed for the one child with a `boundary`.  That works because today's graphs happen
# to contain exactly one synthesizable child — a property of the examples, not of the design.  The
# whole thesis is that a graph can be cut in more than one place, and a discovered cut cannot express
# a second one.
# ==============================================================================================

@pytest.mark.parametrize("tb_cls", _tb_graphs(), ids=lambda c: c.__name__)
def test_naming_the_dut_explicitly_reproduces_the_discovered_cut(tb_cls):
    """S4's gate: every design regenerates identically with the DUT named.

    Discovery becomes the DEFAULT rather than the mechanism, so this must be a no-op for every graph
    that has one synthesizable child — which is all of them today.
    """
    tb = tb_cls(name="tb", sim=Simulation())
    assert tb_top_spec(tb, dut=tb.dut) == tb_top_spec(tb)


def test_the_dut_may_be_named_by_module_or_by_name():
    """Both spellings resolve to the same cut — the sub_comps key and the module itself."""
    tb = _tb()
    assert tb_top_spec(tb, dut=tb.dut.name) == tb_top_spec(tb, dut=tb.dut)


def test_a_named_dut_is_validated_against_the_graph():
    """A DUT that is not in this graph would bind models to ports nothing is wired to.

    That failure would land thousands of cycles into an RTL run with no diagnostic, so it is refused
    here instead — the same reasoning as the unwired-port check above.
    """
    from waveflow.build.hwcodegen import LoweringError

    tb = _tb()
    stranger = type(tb.dut)(name="stranger", sim=tb.sim, mem_dwidth=64)

    with pytest.raises(LoweringError, match="not a sub-component"):
        tb_top_spec(tb, dut=stranger)
    with pytest.raises(LoweringError, match="not one of its sub-components"):
        tb_top_spec(tb, dut="no_such_child")
    # A participant is not a candidate: it has no RTL ports to drive.
    with pytest.raises(LoweringError, match="no `boundary`"):
        tb_top_spec(tb, dut=tb.driver)


def test_discovery_now_says_what_to_do_when_the_graph_is_ambiguous():
    """The fallback's refusal must point at the argument that resolves it.

    Before S4 there was nothing to suggest: "expected exactly one child with a boundary" named the
    problem and left the reader with no move. Now the move exists.
    """
    from waveflow.build.hwcodegen import LoweringError

    tb = _tb()
    tb.add_comp(type(tb.dut)(name="second_dut", sim=tb.sim, mem_dwidth=64))
    with pytest.raises(LoweringError, match=r"tb_top_spec\(tb, dut=\.\.\.\)"):
        tb_top_spec(tb)


def test_the_dut_is_found_by_its_boundary_not_by_kernel_task():
    """Regression: `kernel_task` does NOT identify the DUT — a composite has none (only its
    children do), so both the DUT and the participants answer False. The discriminator is
    `boundary` (RTL ports) vs a DECLARED `bfm_model()` (a TB model)."""
    tb = _tb()
    # Every FreeRunMod now HAS a kernel_task (the base derives one for a leaf), so presence cannot
    # be the discriminator — the canary that used to assert its absence has fired and been checked.
    # What still holds, and is what _find_dut actually keys on:
    with pytest.raises(TypeError, match="composite"):
        tb.dut.kernel_task()            # a composite has no task of its own
    assert hasattr(tb.dut, "boundary")  # ...but it does have RTL ports
    assert not declares_hook(tb.dut, "bfm_model")
    assert declares_hook(tb.driver, "bfm_model") and not hasattr(tb.driver, "boundary")


def test_hasattr_is_not_the_bfm_discriminator_declaration_is():
    """The canary for the S1 migration: `hasattr(c, "bfm_model")` now answers True for EVERYTHING.

    Once `bfm_model()` is a documented hook on `HwModule` (rather than a duck-typed convention), the
    base method exists on every module — including the DUT.  So the probe the TB walk used to
    identify participants would sweep the DUT in with them, and `_find_dut`'s two-way split would
    collapse.  `declares_hook` compares against the base by identity instead, which is the same way
    `FreeRunMod._kind` detects a `run_iter` override.

    This test exists so that reintroducing the `hasattr` spelling fails loudly rather than producing
    a subtly wrong participant set.
    """
    tb = _tb()
    assert hasattr(tb.dut, "bfm_model"), "the base hook exists on every HwModule — that is the trap"
    assert not declares_hook(tb.dut, "bfm_model"), "...but the DUT does not DECLARE one"

    # And the base is a sentinel, not a silent default: calling it says what to do.
    with pytest.raises(NotImplementedError, match="declares no bfm_model"):
        tb.dut.bfm_model()


def test_participants_register_their_endpoints_so_bfm_ports_are_checkable():
    """S1's structural payoff: `BfmModel.ports` names can be VALIDATED, not merely `getattr`-ed.

    `ports` are attribute names in the C++ model's constructor order; `add_endpoint` keys by
    `endpoint.name`.  Those are two different namespaces, and before participants were `HwModule`s
    there was no registry to reconcile them against — so a renamed attribute was a runtime failure
    deep in the walk (or, worse, resolved to some other attribute and modelled the wrong port).
    """
    tb = _tb()
    for part in (tb.driver, tb.done_sink, tb.mem):
        registered = {id(e) for e in part.endpoints.values()}
        assert registered, f"{type(part).__name__} registered no endpoints"
        for attr in part.bfm_model().ports:
            assert id(getattr(part, attr)) in registered, (
                f"{type(part).__name__}.bfm_model() names {attr!r}, which is not a registered "
                f"endpoint")


def test_a_bfm_port_naming_an_unregistered_attribute_fails_at_elaboration():
    """The failure the registry converts: a stale `ports` entry, caught with both namespaces named."""
    from waveflow.build.composite_gen import BfmModel
    from waveflow.build.hwcodegen import LoweringError

    tb = _tb()
    tb.driver.bfm_model = lambda: BfmModel("AxisMaster", ports=("stream_epp",), extra_args=("{}",))
    with pytest.raises(LoweringError, match="no such attribute"):
        tb_top_spec(tb)
