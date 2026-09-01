"""The continuous-capture receiver's **lowering**: what the graph becomes, and whether Vitis takes it.

``plans/t2p_lock_chan.md`` S2, checkpoint 3.  Checkpoints 1 and 2 proved the protocol and the verdict
in pysim; this proves the other half — that the RX pairing reaches a generated top with the memory
ports on the right side of the wrapper seam, and that both bodies synthesize at the II they claim.

**The RX pairing is the whole point of this file.**  The capture is the owner and it *writes*; the
window reader is the requester and it *reads*.  S1 wired the requester to port A by role, which is
right for TX and only for TX — so a lowering that produced the TX port map here would be a design
whose writer was on the port ``bram_t2p.v``'s one-sided ``$error`` does not watch.

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
from waveflow.hw.rf_pingpong_rx import CAPTURE_SCHEMA_CLASSES, N_REGION, RfPingPongRx
from waveflow.toolchain import toolchain

TOP = "rf_pingpong_rx"
WORD_BW = 64
DEPTH = 512
BLK_WORDS = 16
SHIFT = 2
SPW = 4

#: The gated geometry, stated rather than defaulted.  512 words in two regions of 256, blocks of 16 —
#: the plan's own *"the writer fills ``[256, 512)`` while the reader drains ``[0, 256)``"*.
_ELAB = {"bitwidth": WORD_BW, "samp_per_word": SPW, "depth": DEPTH, "shift": SHIFT,
         "blk_words": BLK_WORDS}


def _rx():
    return elaborate(RfPingPongRx, dict(_ELAB), name=TOP)


# ---------------------------------------------------------------------------
# What the graph lowers to
# ---------------------------------------------------------------------------

def test_the_receiver_lowers_as_a_composite_kernel():
    """``check()`` runs the real generator, so this is the rules and not a restatement of them."""
    ok, msg = check(_rx(), COMPOSITE_KERNEL)
    assert ok, msg


def test_the_memory_ports_stay_on_the_boundary_and_the_lock_streams_do_not():
    """Four boundary ports and four internal channels.

    Get the split wrong in either direction and nothing says so until much later: a ``BramIF``
    counted as an internal edge makes the kernel's memory ports vanish into a FIFO that does not
    exist, and a lock stream counted as a boundary port makes the kernel demand two AXI-Stream pins a
    testbench has to drive.
    """
    spec = composite_top_spec(_rx(), width=WORD_BW)
    assert [(p.name, p.kind) for p in spec.ports] == [
        ("samp_in", "axis_in"), ("buf_w", "bram"), ("buf_r", "bram"), ("w_out", "axis_out")]
    assert sorted(c.name for c in spec.channels) == ["dense", "lock_if_cmd", "lock_if_resp", "rdy"]
    # The wrapper hides the memory ports, so from outside the design is two AXI-Stream pins.
    assert [p.name for p in spec.pin_ports] == ["samp_in", "w_out"]


def test_the_CAPTURE_is_on_port_A_because_it_is_the_one_that_WRITES():
    """**The RX inversion, at the port map.**

    S1 routed the requester to port A by role.  Here the requester *reads*, so it belongs on port B,
    and the owner — which writes — belongs on A.  ``bram_t2p.v``'s ``$error`` is written one-sided
    (*A writes while B touches the same address*), so a lowering that kept the TX map would put this
    design's writer on the port the memory's only real check does not watch.
    """
    from waveflow.build.wrapper_gen import bram_hazard_manifest

    comp = _rx()
    spec = composite_top_spec(comp, width=WORD_BW)
    assert comp.capture.lock.mem_ep.interface is comp.lock.wr_if
    assert comp.window.lock.mem_ep.interface is comp.lock.rd_if
    mem = bram_hazard_manifest(comp, spec)["memories"][0]
    assert mem["write"]["addr"] == "buf_w_addr_a", (
        "the hazard scan must watch the CAPTURE's port as the writer; if it names the reader's, the "
        "RTL check is pointed at the wrong half of the design")
    assert mem["read"]["addr"] == "buf_r_addr_a"


def test_the_window_port_carries_TLAST_and_the_input_does_not():
    """A window is a frame; a stream of blocks is not.

    ``w_out`` is a :class:`~waveflow.hw.interface.FramedStreamIFMaster` because a host reads one
    window as one DMA transfer and has to learn where it ends without being told a length.
    ``samp_in`` is a plain stream: a converter never stops, so there is no boundary to mark.
    """
    text = render_top(composite_top_spec(_rx(), width=WORD_BW))
    assert "hls::stream<streamutils::axi4s_word<64> >& w_out" in text
    assert "hls::stream<ap_uint<64> >& samp_in" in text


def test_the_three_tasks_take_their_lock_arguments_adjacent():
    """``lock`` occupies one name in each signature and three arguments in the C++.

    Adjacent and in ``physical_endpoints()`` order, which is why both hand-written bodies read
    ``(buf, cmd, resp)`` together.  The two are instantiated from **one** set of template arguments,
    so a window task told a different geometry from its capture is not expressible.
    """
    text = render_top(composite_top_spec(_rx(), width=WORD_BW))
    assert (f"hls::task t1(pingpong_capture_task<{WORD_BW}, {DEPTH}, {N_REGION}, {BLK_WORDS}>, "
            f"dense, buf_w, lock_if_cmd, lock_if_resp, rdy);") in text
    assert (f"hls::task t2(pingpong_window_task<{WORD_BW}, {DEPTH}, {N_REGION}, {BLK_WORDS}>, "
            f"rdy, buf_r, lock_if_cmd, lock_if_resp, w_out);") in text


def test_the_wrapper_joins_both_memory_ports_from_one_add_if():
    """``add_if(lock)`` alone is enough for the wrapper to find both wires.

    A composite that had to remember two ``add_rtl_if`` calls could forget one, and a dangling
    ``bram`` port is refused only after codegen — by which point the message names a port rather than
    the missing call.
    """
    comp = _rx()
    spec = composite_top_spec(comp, width=WORD_BW)
    assert sorted(comp.rtl_ifs) == sorted([comp.lock.wr_if.name, comp.lock.rd_if.name])
    v = render_wrapper(wrapper_spec(comp, spec))
    assert f"module {TOP}_top (" in v
    for port in ("buf_w", "buf_r"):
        assert f".{port}_Addr_A(" in v, f"the wrapper leaves {port} dangling"
    assert "bram_t2p" in v, "the memory is not instantiated beside the kernel"


# ---------------------------------------------------------------------------
# Vitis
# ---------------------------------------------------------------------------

def _stage(tmp_path: Path) -> Path:
    """Write the whole compilable tree: headers, both task bodies, the top, and its tcl."""
    from waveflow.build.build import BuildConfig, BuildDag
    from waveflow.build.streamutils import (
        MemLockStep,
        MemMgrStep,
        RfPingPongStep,
        RfShotBufStep,
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
    # The re-layout body is RfShotBufStep's -- it is Stage A's and shared, not this design's.
    dag.add(RfShotBufStep(output_dir=inc))
    dag.add(MemLockStep(output_dir=inc))
    dag.add(RfPingPongStep(output_dir=inc))
    for cls in [*LOCK_SCHEMA_CLASSES, *CAPTURE_SCHEMA_CLASSES]:
        dag.add(DataSchemaStep(cls, word_bw_supported=[WORD_BW], include_dir=inc))
    # The serializers the re-layout body calls.  The SLOT element is the converter's container width
    # and the DENSE element the effective one; at 14-in-16 they differ, which is what makes the first
    # stage a conversion rather than a pair of wires.
    dag.add(ArrayUtilsStep(slot_elem_type(word, inc), [WORD_BW]))
    dag.add(ArrayUtilsStep(dense_elem_type(word, inc), [WORD_BW]))
    results = dag.run(BuildConfig(root_dir=tmp_path), force=True)
    failed = [n for n, r in results.items() if not r.success]
    assert not failed, f"header generation failed: {failed}"

    gen = tmp_path / "gen"
    gen.mkdir(parents=True, exist_ok=True)
    (gen / f"{TOP}.cpp").write_text(render_top(composite_top_spec(_rx(), width=WORD_BW)),
                                    encoding="utf-8")
    # `config_rtl -reset state` is here for the capture's statics.  Unlike TX's player this owner
    # READS before it writes -- its first act is a blocking stream read -- so it is on the safe side
    # of the reset trap; the setting is kept because the statics are still state a reset should
    # clear, and because a build that differed from the TX one only in this would be a difference
    # nobody could explain later.
    (tmp_path / f"{TOP}.tcl").write_text(
        render_tcl(TOP, part="xczu48dr-ffvg1517-2-e", period_ns=4,
                   solution_config=("config_rtl -reset state",)),
        encoding="utf-8")
    return tmp_path


@pytest.mark.vitis
def test_the_receiver_csynthesizes_at_ii_1(tmp_path):
    """Vitis accepts both lock-aware bodies, at II=1 on every pipelined loop.

    The II is **achieved**, read out of the report, and the loop names are **discovered** by label:
    Vitis names an unlabelled loop ``VITIS_LOOP_<line>_1`` and nests that into its children, so a
    spelled name stops matching on a comment edit — and a gate that skipped on a miss would read as a
    pass.

    ``store_block`` is the one that matters most.  It reads its input unconditionally and writes the
    memory *conditionally*, which is the shape that makes a drop possible without back-pressuring an
    ADC — and it is exactly the shape one might expect to cost a cycle.  It does not.
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
             for label in ("store_block", "drain_window", "await_grant")}
    for label, mods in found.items():
        assert len(mods) == 1, (
            f"expected exactly one synthesized module for the {label!r} loop, found {mods}. An "
            f"empty list means the label moved or the loop stopped being pipelined — and a gate "
            f"that skipped here would read as a pass.")
        module = mods[0]
        loops = module_loops(report, module)
        assert loops, f"{module} reports no pipelined loop at all"
        assert loop_pipeline_ii(report, module, loops[0]) == 1, (
            f"{module}.{loops[0]} does not achieve II=1. One cycle per element is the throughput "
            f"claim of both sides of this lock; do not re-record it without diagnosing why.")
