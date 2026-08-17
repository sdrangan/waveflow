"""Can a design **reuse a composite**, and does the generator flatten it correctly?

``hls::task`` has no hierarchy: a generated top is a flat list of tasks joined by channels.  Until
``rf_blk_delay`` every composite in the repo had only leaf children, so the question never came up —
and a design that reaches its converter through ``RfSampBufRx`` and ``RfSampBufTx`` is a composite of
composites by construction, which is the pattern ``plans/adc_model.md`` makes the default.

Two things have to hold, and the second is the one that bit:

1. **The tasks flatten.**  Five leaves, not three children, and the channels inside the reused
   buffers come up with them.
2. **The names stay distinct.**  An edge name is the interface's name minus its own owner's prefix,
   so two instances of a module contribute channels with *identical* names — both sample buffers call
   their progress channel ``wr``.  Emitted as-is that is one C++ variable declared twice with the two
   buffers' tasks sharing it.
"""
from __future__ import annotations

import pytest

from waveflow.build.composite_gen import composite_top_spec, kernel_tasks
from waveflow.build.elaborate import elaborate
from waveflow.build.hwcodegen import LoweringError
from waveflow.hw.rf_samp_buf import RfSampBufRx
from waveflow.simulation.simulation import Simulation

from examples.rf_blk_delay.rf_blk_delay import RfBlkDelayLoop
from examples.rf_blk_delay.rf_blk_delay_build import elab_params


@pytest.fixture(scope="module")
def spec():
    return composite_top_spec(elaborate(RfBlkDelayLoop, elab_params(), name="rf_blk_delay"),
                              width=64)


def test_a_two_level_design_lowers_to_a_flat_list_of_leaf_tasks(spec):
    """Three ``add_comp``\\ s become **five** tasks, in dataflow order."""
    fns = [t.task_fn for t in spec.tasks]
    assert fns == ["rf_samp_buf_ingress_task", "rf_samp_buf_capture_task", "blk_delay_task",
                   "rf_samp_buf_loader_task", "rf_samp_buf_player_task"], fns


def test_the_reused_composites_own_channels_come_up_with_their_tasks(spec):
    """Six channels: three the loop declares, and three that belong to the buffers.

    Without the hoist the buffers' tasks would be handed arguments naming nothing, and the failure
    would read as an unwired port rather than as a missing level of hierarchy.
    """
    names = sorted(c.name for c in spec.channels)
    assert names == ["cmd", "load", "rx_wr", "samp", "tx_rd", "tx_wr"], names


def test_a_hoisted_channel_is_qualified_by_the_child_it_came_from(spec):
    """**The collision, pinned.**  Both buffers call their fill channel ``wr``.

    The RX one joins ingress to capture and the TX one joins loader to player; they are different
    channels and must stay different variables.  Asserting the *wiring* rather than only the names,
    because two correctly-named channels crossed over would still be wrong.
    """
    by_name = {c.name: c for c in spec.channels}
    assert "wr" not in by_name, "a hoisted channel kept its unqualified name and will collide"
    ing, cap, _dly, loader, player = range(5)
    assert (by_name["rx_wr"].master_task, by_name["rx_wr"].slave_task) == (ing, cap)
    assert (by_name["tx_wr"].master_task, by_name["tx_wr"].slave_task) == (loader, player)
    assert (by_name["tx_rd"].master_task, by_name["tx_rd"].slave_task) == (player, loader)


def test_two_channels_with_one_name_are_refused_rather_than_emitted_twice():
    """The guard, exercised on a graph deliberately built to collide.

    Two ``RfSampBufRx``\\ es in one top: each hoists a channel named ``wr``, and the qualifier is what
    keeps them apart.  Bypassing it by assigning ``internal_edges`` directly shows the generator
    refuses the duplicate rather than emitting a C++ variable twice — which would compile as a
    redeclaration error at best and cross-wire the two buffers at worst.
    """
    comp = elaborate(RfBlkDelayLoop, elab_params(), name="dup")
    edges = list(comp.internal_edges)
    import dataclasses
    comp.internal_edges = [dataclasses.replace(e, name="wr") if e.name.endswith("_wr") else e
                           for e in edges]
    with pytest.raises(LoweringError, match="two internal channels named 'wr'"):
        composite_top_spec(comp, width=64)


def test_the_memories_of_both_buffers_reach_the_one_wrapper(spec):
    """Four ``bram`` ports and two memories — one per buffer, never shared.

    ``rtl_mods`` aggregates over the flattened hierarchy for the same reason the channels do: there is
    one wrapper per generated top, and it has to instantiate every memory the design uses.
    """
    comp = elaborate(RfBlkDelayLoop, elab_params(), name="rf_blk_delay")
    assert len(comp.rtl_mods) == 2, f"expected both buffers' memories, got {list(comp.rtl_mods)}"
    assert len(comp.rtl_ifs) == 4, f"expected four wrapper wires, got {list(comp.rtl_ifs)}"
    bram = [p.name for p in spec.ports if p.kind == "bram"]
    assert bram == ["rx_buf_w", "rx_buf_r", "tx_buf_w", "tx_buf_r"], bram


def test_flattening_is_a_no_op_for_a_composite_whose_children_are_leaves():
    """The property that makes this change safe for every design that came before it.

    ``RfSampBufRx``'s children are two leaves, so flattening returns exactly what the unflattened walk
    returned.  If this ever stops holding, every existing generated top is at risk, not just the new
    one.
    """
    rx = RfSampBufRx(name="flat", sim=Simulation(), bitwidth=16, samp_per_word=1, depth=1024,
                     horizon_margin=16)
    assert kernel_tasks(rx) == rx.ordered_subcomps == [rx.ingress, rx.capture]


def test_the_graph_walk_still_sees_one_level_so_a_testbench_can_find_its_dut():
    """Why the flattening lives in the generator and not on the property.

    ``ordered_subcomps`` is also how ``tb_top_spec`` finds the DUT among a testbench's children.
    Flattened there, the node it is looking for would have dissolved into the leaves it contains —
    which is exactly what happened when the recursion was tried on the property, and it took out
    fifteen testbench-spec tests at once.
    """
    loop = RfBlkDelayLoop(name="lvl", sim=Simulation(), **elab_params())
    assert loop.ordered_subcomps == [loop.rx, loop.dut, loop.tx]
    assert len(kernel_tasks(loop)) == 5
