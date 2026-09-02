"""The unified transmitter's **lowering**: what the merge becomes, and whether Vitis takes it.

``plans/rf_shot_unify.md`` Stage A.  The pysim gates prove the merge behaves like both predecessors;
this proves the other half — that it reaches a generated top with the memory ports on the right side
of the wrapper seam, and that both bodies synthesize at the II they claim.

**The count is the point.**  ``RfShotTx`` instantiates five tasks and hand-wires seven internal
channels plus two ``BramIF``\\ s.  This instantiates three and wires three plus one ``add_if(lock)``.
The four that vanished existed only to move samples between tasks the lock made unnecessary.

The Vitis half is one test and it is marked ``vitis``.  A failed csynth is a real failure; the skip
is only for Vitis not being installed.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from waveflow.build.codegen_check import check
from waveflow.build.composite_gen import composite_top_spec, render_tcl, render_top
from waveflow.build.elaborate import elaborate
from waveflow.build.wrapper_gen import render_wrapper, wrapper_spec
from waveflow.hw.codegen_targets import COMPOSITE_KERNEL
from waveflow.hw.locked_mem import LOCK_SCHEMA_CLASSES
from waveflow.hw.rf_shot_tx import SHOT_TX_SCHEMA_CLASSES
from waveflow.hw.rf_shot_tx_unified import UNIFIED_TX_SCHEMA_CLASSES, RfShotTxUnified
from waveflow.toolchain import toolchain

TOP = "rf_shot_tx_unified"
WORD_BW = 64
SPW = 4
DEPTH = 256
NWORD = 64
BLK_WORDS = 16
#: The region at the **top** of the memory — ``base + offset`` is the shape of the byte-versus-word
#: bug, so a build that only ever loaded at zero would be measuring nothing.
BASE = DEPTH - NWORD
SHIFT = 2

_ELAB = {"bitwidth": WORD_BW, "samp_per_word": SPW, "depth": DEPTH, "nword": NWORD,
         "base": BASE, "shift": SHIFT, "blk_words": BLK_WORDS}


def _dut():
    return elaborate(RfShotTxUnified, dict(_ELAB), name=TOP)


# ---------------------------------------------------------------------------
# What the graph lowers to
# ---------------------------------------------------------------------------

def test_the_merged_design_lowers_as_a_composite_kernel():
    """``check()`` runs the real generator, so this is the rules and not a restatement of them."""
    ok, msg = check(_dut(), COMPOSITE_KERNEL)
    assert ok, msg


def test_five_tasks_became_three_and_seven_channels_became_three():
    """**The merge, counted at the port map.**

    ``RfShotTx``: five tasks; ``pay rep rdy_load rdy_play dense samp done`` hand-wired, plus
    ``bufw`` and ``bufr``.  Here: three tasks; ``rep done samp`` plus one ``add_if(lock)`` that files
    the two lock streams as internal edges and both memory wires as wrapper wires.

    ``rep`` and ``done`` survive because the lock has no opinion about them — one says *what to play*
    and the other says *a finite shot has finished*, and neither is a question about who may touch
    which addresses.
    """
    spec = composite_top_spec(_dut(), width=WORD_BW)
    assert len(spec.tasks) == 3
    assert [(p.name, p.kind) for p in spec.ports] == [
        ("s_in", "axis_in"), ("resp_out", "axis_out"),
        ("buf_w", "bram"), ("buf_r", "bram"), ("samp_out", "axis_out")]
    assert sorted(c.name for c in spec.channels) == [
        "done", "lock_if_cmd", "lock_if_resp", "rep", "samp"]
    # The wrapper hides the memory ports, so from outside the design is three AXI-Stream pins.
    assert [p.name for p in spec.pin_ports] == ["s_in", "resp_out", "samp_out"]


def test_both_task_bodies_are_instantiated_from_one_geometry():
    """``lock`` occupies one name in each signature and three arguments in the C++.

    Adjacent and in ``physical_endpoints()`` order, which is why both hand-written bodies read
    ``(buf, cmd, resp)`` together.  And the two halves take the same ``BASE`` and ``NW``: a player
    told a different region from its loader is a design whose two ends are each individually correct.
    """
    text = render_top(composite_top_spec(_dut(), width=WORD_BW))
    assert (f"hls::task t0(shot_tx_loader_task<{WORD_BW}, {DEPTH}, {NWORD}, {SPW}, {BASE}>, "
            f"s_in, done, buf_w, lock_if_cmd, lock_if_resp, rep, resp_out);") in text
    assert (f"hls::task t1(shot_tx_player_task<{WORD_BW}, {DEPTH}, {NWORD}, {BASE}, {BLK_WORDS}>, "
            f"buf_r, lock_if_cmd, lock_if_resp, rep, done, samp);") in text


def test_the_command_and_response_ports_carry_TLAST_and_the_sample_port_does_not():
    """A frame is a transaction; a playout is not.

    ``s_in`` and ``resp_out`` are framed because a host reads and writes each as one DMA transfer and
    has to learn where it ends without being told a length.  ``samp_out`` is a plain stream: a
    converter never stops, so there is no boundary to mark.
    """
    text = render_top(composite_top_spec(_dut(), width=WORD_BW))
    for port in ("s_in", "resp_out"):
        assert f"hls::stream<streamutils::axi4s_word<64> >& {port}" in text
    assert "hls::stream<ap_uint<64> >& samp_out" in text


def test_the_loader_writes_port_A_and_the_player_reads_port_B():
    """``bram_t2p.v``'s ``$error`` is one-sided — *A writes while B touches the same address*.

    The lock routes by declared direction rather than by role, which is what lets TX and RX share it.
    On TX the requester writes, so it lands on A and the memory's only real check watches the right
    half of the design.
    """
    from waveflow.build.wrapper_gen import bram_hazard_manifest

    comp = _dut()
    spec = composite_top_spec(comp, width=WORD_BW)
    assert comp.load.lock.mem_ep.interface is comp.lock.wr_if
    assert comp.play.lock.mem_ep.interface is comp.lock.rd_if
    mem = bram_hazard_manifest(comp, spec)["memories"][0]
    assert mem["write"]["addr"] == "buf_w_addr_a" and mem["read"]["addr"] == "buf_r_addr_a"


def test_the_wrapper_joins_both_memory_ports_from_one_add_if():
    """``add_if(lock)`` alone is enough for the wrapper to find both wires.

    ``RfShotTx`` needs two ``add_rtl_if`` calls and could forget one; a dangling ``bram`` port is
    refused only after codegen, by which point the message names a port rather than the missing call.
    """
    comp = _dut()
    spec = composite_top_spec(comp, width=WORD_BW)
    assert sorted(comp.rtl_ifs) == sorted([comp.lock.wr_if.name, comp.lock.rd_if.name])
    v = render_wrapper(wrapper_spec(comp, spec))
    assert f"module {TOP}_top (" in v
    for port in ("buf_w", "buf_r"):
        assert f".{port}_Addr_A(" in v, f"the wrapper leaves {port} dangling"
    assert "bram_t2p" in v, "the memory is not instantiated beside the kernel"


def test_the_predecessors_are_untouched():
    """**Stage A deletes nothing**, and this is the assertion that says so.

    If the merge turns out harder than it looks, the working designs must still be there — so both
    still import, still lower, and their own gates still run.
    """
    from waveflow.hw.rf_shot_loop import RfShotTxLoop
    from waveflow.hw.rf_shot_tx import RfShotTx

    for cls, name in ((RfShotTx, "rf_shot_tx"), (RfShotTxLoop, "rf_shot_tx_loop")):
        assert cls.cpp_kernel_name == name
        ok, msg = check(elaborate(cls, {}, name=name), COMPOSITE_KERNEL)
        assert ok, f"{name} stopped lowering: {msg}"


# ---------------------------------------------------------------------------
# Vitis
# ---------------------------------------------------------------------------

def _stage(tmp_path: Path) -> Path:
    """Write the whole compilable tree: headers, both task bodies, the top, and its tcl."""
    from waveflow.build.build import BuildConfig, BuildDag
    from waveflow.build.streamutils import (
        MemLockStep,
        MemMgrStep,
        RfShotBufStep,
        RfShotTxUnifiedStep,
        StreamUtilsStep,
    )
    from waveflow.hw.arrayutils import ArrayUtilsStep
    from waveflow.hw.dataschema import DataSchemaStep
    from waveflow.hw.rf_relayout import dense_elem_type, slot_elem_type
    from waveflow.hw.rfdc_samp_word import Rfsoc4x2SampWord

    word = Rfsoc4x2SampWord.specialize(samp_per_word=SPW)
    inc = "include"
    dag = BuildDag()
    dag.add(StreamUtilsStep(output_dir=inc))
    # `render_top` includes memmgr.hpp unconditionally, so it must be beside the sources even for a
    # design with no m_axi port at all.
    dag.add(MemMgrStep(output_dir=inc))
    # The re-layout body is Stage A's and shared; the two merged bodies are this design's.
    dag.add(RfShotBufStep(output_dir=inc))
    dag.add(RfShotTxUnifiedStep(output_dir=inc))
    dag.add(MemLockStep(output_dir=inc))
    # BOTH schema lists: the header and the verdict are still rf_shot_tx's at Stage A, and the play
    # command is the merged design's own.  See the ownership decision in plans/rf_shot_unify.md.
    for cls in [*SHOT_TX_SCHEMA_CLASSES, *LOCK_SCHEMA_CLASSES, *UNIFIED_TX_SCHEMA_CLASSES]:
        dag.add(DataSchemaStep(cls, word_bw_supported=[WORD_BW], include_dir=inc))
    # The serializers the re-layout body calls.  The SLOT element is the converter's container width
    # and the DENSE element the effective one; at 14-in-16 they differ, which is what makes the last
    # stage a conversion rather than a pair of wires.
    dag.add(ArrayUtilsStep(slot_elem_type(word, inc), [WORD_BW]))
    dag.add(ArrayUtilsStep(dense_elem_type(word, inc), [WORD_BW]))
    results = dag.run(BuildConfig(root_dir=tmp_path), force=True)
    failed = [n for n, r in results.items() if not r.success]
    assert not failed, f"header generation failed: {failed}"

    gen = tmp_path / "gen"
    gen.mkdir(parents=True, exist_ok=True)
    (gen / f"{TOP}.cpp").write_text(render_top(composite_top_spec(_dut(), width=WORD_BW)),
                                    encoding="utf-8")
    # `config_rtl -reset state` is not decoration: the PLAYER writes before it reads, which is the
    # reset trap, and the pragma alone did not close it under Vitis 2025.1 in rf_repeat_play.
    (tmp_path / f"{TOP}.tcl").write_text(
        render_tcl(TOP, part="xczu48dr-ffvg1517-2-e", period_ns=4,
                   solution_config=("config_rtl -reset state",)),
        encoding="utf-8")
    return tmp_path


@pytest.mark.vitis
def test_the_merged_design_csynthesizes_at_ii_1(tmp_path):
    """Vitis accepts both merged bodies, at II=1 on every pipelined loop.

    The II is **achieved**, read out of the report, and the loop names are **discovered** by label:
    Vitis names an unlabelled loop ``VITIS_LOOP_<line>_1`` and nests that into its children, so a
    spelled name stops matching on a comment edit — and a gate that skipped on a miss would read as a
    pass.

    ``play_chunk`` is the one that carries the merge.  It gained a pass counter and an exit condition
    over ``rf_shot_loop``'s, and both are register reads outside the loop body — so the shape one
    might expect to cost a cycle does not.
    """
    from waveflow.utils.csynthparse import loop_pipeline_ii, module_loops

    if not toolchain.find_vitis_path():
        pytest.skip("Vitis not found.")
    root = _stage(tmp_path)
    try:
        toolchain.run_vitis_hls(root / f"{TOP}.tcl", work_dir=root)
    except RuntimeError as exc:
        pytest.skip(f"Vitis execution unavailable: {exc}")
    except subprocess.CalledProcessError as exc:
        pytest.fail(f"csynth of {TOP} failed:\n{exc.stdout}\n{exc.stderr}")

    report = root / f"{TOP}_proj" / "solution1" / "syn" / "report"
    assert report.is_dir(), f"csynth reported success but wrote no report dir at {report}"

    found = {label: [p.stem[: -len("_csynth")] for p in report.glob("*_csynth.xml")
                     if p.stem.endswith(f"Pipeline_{label}_csynth")]
             for label in ("take_shot", "drain_tail", "await_grant", "play_chunk")}
    for label, mods in found.items():
        assert len(mods) == 1, (
            f"expected exactly one synthesized module for the {label!r} loop, found {mods}. An "
            f"empty list means the label moved or the loop stopped being pipelined — and a gate "
            f"that skipped here would read as a pass.")
        module = mods[0]
        loops = module_loops(report, module)
        assert loops, f"{module} reports no pipelined loop at all"
        assert loop_pipeline_ii(report, module, loops[0]) == 1, (
            f"{module}.{loops[0]} does not achieve II=1. One cycle per word is the throughput claim "
            f"of both sides of this lock; do not re-record it without diagnosing why.")
