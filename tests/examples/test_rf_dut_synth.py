"""The RF pass-through DUT: synthesizable, synthesized, and proved at RTL.

``plans/adc_model.md`` staging item 2, opened at the digital-logic end. ``RfSampPassThrough`` is the
module that sits between the two converter ports in the loopback; here it is cut alone, between
generic AXI-Stream BFMs, with no converter and no RF edges.

**The same module, a different cut.** That is the property ``plans/design_cut.md`` asserts, and this
file is the first place it is exercised rather than stated: nothing about the DUT changes between the
pysim loopback graph and this one — only what is attached beyond its ports.

The layers, and what each can and cannot say:

- ``check(..., "composite_kernel")`` — the graph lowers. Says nothing about the body (see
  :func:`test_the_task_body_extracts`, which is the check that actually would have caught the two
  extractor refusals this step hit).
- pysim — the relay is bit-identical in Python. No toolchain.
- csynth (``-m vitis``) — the RTL exists **and contains a datapath**. csynth reporting success is
  not evidence: a nested-struct-by-value argument silently DCEs a kernel and still reports OK.
- XSI (``-m xsi``, in ``test_xsi_bfm.py``) — the real RTL relays the words, bit-identical, at a
  measured cycle count.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from examples.rf_loopback.rf_dut_build import (
    TOP,
    XSI_NBURST,
    XSI_NWORDS_BLK,
    RfPassThroughTB,
    input_bursts,
    write_scenario,
)
from examples.rf_loopback.rf_loopback import RfSampBlockRelay, RfSampPassThrough
from waveflow.build.codegen_check import check
from waveflow.simulation.simulation import Simulation

ROOT = Path(__file__).resolve().parents[2] / "examples" / "rf_loopback"
PROJ = ROOT / f"{TOP}_proj" / "solution1" / "syn" / "verilog"


# ---------------------------------------------------------------------------
# The module is synthesizable
# ---------------------------------------------------------------------------

def test_the_graph_lowers():
    ok, msg = check(RfSampPassThrough, "composite_kernel")
    assert ok, msg


def test_the_task_body_extracts():
    """The check the graph-level one does not make.

    ``check(mod, "composite_kernel")`` runs ``composite_top_spec``, which walks the graph and never
    touches ``run_iter``. So it answered ``True`` while the body could not be extracted at all — two
    separate refusals were hiding behind that ``True``, and this is the assertion that would have
    shown them.

    The subject is the **block relay child**, not the composite: a composite has no body of its own,
    and the ingress hands over a hand-written one.
    """
    from waveflow.build.hwgen import task_files_to_str

    files = task_files_to_str(RfSampBlockRelay)
    body = files["rf_samp_relay_task.h"]
    assert "static void rf_samp_relay_task(" in body
    # It relays through the generated array (de)serializers -- never hand-rolled packing.
    assert "read_stream<64>(blk_in)" in body and "write_stream<64>(s_out)" in body
    assert '#include "u_int64_array.h"' in body


def test_the_composite_has_no_body_of_its_own():
    """A composite's codegen IS its sub-component graph, and asking it for a task must say so."""
    from waveflow.build.hwgen import task_files_to_str

    with pytest.raises(Exception, match="composite"):
        task_files_to_str(RfSampPassThrough)


def test_the_ingress_hands_over_a_hand_written_body():
    """The ingress's C++ is a word relay; its ``run_iter`` is a burst relay, and that is not a bug.

    ``StreamIFSlave.get`` pops one burst and truncates it, so a word-granular Python body would
    discard 63 of every 64 words — the relay the hardware needs has no pysim expression. The module
    therefore overrides ``kernel_task()`` and leaves ``run_iter`` as the block-granular twin.
    """
    from examples.rf_loopback.rf_dut_build import FIXED_TASK_BODIES
    from examples.rf_loopback.rf_loopback import RfSampIngress

    kt = RfSampIngress(name="i", sim=Simulation()).kernel_task()
    assert kt.task_fn == "rf_samp_ingress_task" and kt.header in FIXED_TASK_BODIES
    assert kt.signature == ("s_in", "w_out")
    src = (ROOT / "src" / kt.header).read_text(encoding="utf-8")
    assert "w_out.write(s_in.read());" in src, (
        "the ingress body must be ONE word in, ONE word out — anything that buffers stops reading "
        "the boundary port, which is the defect this task exists to remove")


def test_the_pysim_counter_is_marked_sim_only():
    """Guard the fix, not just its effect.

    ``n_blk`` is instrumentation; inlining ``self.n_blk += 1`` in ``run_iter`` is what the
    implicit-capture rule rejects. If someone moves it back inline, extraction breaks — but only in a
    toolchain-gated test unless this one exists.
    """
    assert getattr(RfSampBlockRelay.count_burst, "_is_sim_only", False)


# ---------------------------------------------------------------------------
# pysim
# ---------------------------------------------------------------------------

def test_pysim_relay_is_bit_identical(tmp_path):
    tb = RfPassThroughTB(name="tb", sim=Simulation())
    write_scenario(tmp_path)
    tb.driver.root = tmp_path
    tb.sim.run_sim()
    got = np.concatenate(tb.sink.words)
    np.testing.assert_array_equal(got, np.concatenate(input_bursts()))
    assert tb.dut.n_blk == XSI_NBURST


def test_the_scenario_exercises_the_full_word_width():
    """Guard the discriminator: a pattern that never sets the high bits would let a design that
    truncated to 32 bits pass."""
    words = np.concatenate(input_bursts())
    assert (words >> 32).max() > 0, "the scenario no longer exercises the upper half of a word"
    assert words.size == XSI_NBURST * XSI_NWORDS_BLK


def test_the_dut_is_the_same_module_the_loopback_uses():
    """The cut is a build choice. Both graphs hold the *same class* with the same ports."""
    from examples.rf_loopback.rf_loopback import RfLoopbackTB

    loop = RfLoopbackTB(name="l", sim=Simulation())
    alone = RfPassThroughTB(name="a", sim=Simulation())
    assert type(loop.dut) is type(alone.dut) is RfSampPassThrough
    # The composite's ports live on its children, so compare the BOUNDARY (names and endpoint types)
    # rather than its own endpoint dict, which a composite does not populate.
    assert [(n, type(e).__name__) for n, e in loop.dut.boundary] == \
        [(n, type(e).__name__) for n, e in alone.dut.boundary]
    assert [n for n, _e in loop.dut.boundary] == ["s_in", "s_out"]


# ---------------------------------------------------------------------------
# csynth — and what it does NOT prove
# ---------------------------------------------------------------------------

@pytest.mark.vitis
def test_csynth_produces_a_real_datapath():
    """csynth OK is not evidence; the module set is.

    A kernel whose argument was DCE'd still reports success and still writes a top — with nothing
    under it. So this asserts the RTL *contains the work*: the task module, the two pipelined loops
    (the read and the write), and the block buffer between them.
    """
    from waveflow.build.build import BuildConfig
    from examples.rf_loopback.rf_dut_build import build_rf_dut_dag

    dag = build_rf_dut_dag()
    results = dag.run(BuildConfig(root_dir=ROOT, params={}), through="csynth")
    failed = [n for n, r in results.items() if not r.success]
    assert not failed, f"build steps failed: {failed}"

    assert PROJ.is_dir(), f"no RTL at {PROJ}"
    mods = {p.stem for p in PROJ.glob("*.v")}
    assert TOP in mods, f"no top module in {sorted(mods)}"
    # BOTH task modules, because a composite that DCE'd one of them would still report OK and still
    # write a top. The ingress is the one that keeps the boundary port drained; a top with only the
    # relay in it is exactly the design this change replaced.
    for task in (f"{TOP}_rf_samp_ingress_task_64_s", f"{TOP}_rf_samp_relay_task"):
        assert task in mods, (
            f"{task} is missing from the RTL — the kernel was optimized away: {sorted(mods)}")
    pipelines = [m for m in mods if "Pipeline_VITIS_LOOP" in m]
    assert len(pipelines) >= 2, (
        f"expected the read and write loops as pipelined modules, found {pipelines}")
    assert any("RAM" in m for m in mods), (
        f"expected a block buffer between the two loops, found {sorted(mods)}")
    # The internal channel is one block deep AS RTL. Unlike a boundary port, this depth declaration
    # is honoured (HLS 214-387 applies to top-level arguments only), and the whole overlap depends
    # on it: the FIFO is what the ingress hands a block off into.
    assert f"{TOP}_fifo_w64_d64_A" in mods, (
        f"no depth-64 internal FIFO in the RTL — the declared channel depth did not reach the "
        f"hardware: {sorted(mods)}")


@pytest.mark.vitis
def test_the_rtl_file_list_matches_the_rtl_on_disk():
    """A stale ``.f`` plus a cached ``xsimk.dll`` is how an XSI run goes green proving nothing."""
    from waveflow.build.composite_gen import render_rtl_f

    if not PROJ.is_dir():
        pytest.skip(f"no csynth RTL at {PROJ}")
    committed = (ROOT / "xsi" / f"rtl_{TOP}.f").read_text(encoding="utf-8").replace("\r\n", "\n")
    assert render_rtl_f(TOP, ROOT) == committed


# ---------------------------------------------------------------------------
# The XSI workspace
# ---------------------------------------------------------------------------

def test_the_workspace_carries_the_framework_library():
    """The gates compile the COMMITTED copies, so a new workspace must have them."""
    src = Path(__file__).resolve().parents[2] / "waveflow" / "build" / "xsi"
    ws = ROOT / "xsi"
    for name in ("xsi_bfm.h", "xsi_simobj.h", "xsi_channel.h", "xsi_bundle.h", "run.bat", "run.sh"):
        assert (ws / name).is_file(), f"{name} missing from {ws}"
        assert (ws / name).read_text(encoding="utf-8").replace("\r\n", "\n") == \
            (src / name).read_text(encoding="utf-8").replace("\r\n", "\n"), \
            f"{name} has drifted from waveflow/build/xsi/"


def test_the_workspace_now_carries_the_converter_headers():
    """**Inverted deliberately.** This assertion used to say the opposite.

    It was a scope guard: while this cut used only generic BFMs, a converter header in the workspace
    would have been a header nothing included, and the guard said so. The RF loopback testbench now
    shares this workspace and names ``RfdcAdcMaster`` / ``RfFileSource``, so the headers belong here
    and ``XsiHarnessStep`` copies them. Inverted rather than deleted, because "which headers does
    this workspace carry, and why" is the question that went stale — not the answer.
    """
    ws = ROOT / "xsi"
    for name in ("xsi_rfdc.h", "xsi_rfdc_samp.h", "xsi_rf_block.h"):
        assert (ws / name).is_file(), f"{name} missing from {ws}"


def test_generated_top_and_harness_are_present():
    for f in (f"{TOP}_ports.h", f"{TOP}_tb_harness.h", f"{TOP}_bfm_tb.cpp"):
        assert (ROOT / "xsi" / f).is_file(), f"{f} was not generated"
    assert (ROOT / "gen" / f"{TOP}.cpp").is_file()
    # One generated body and one copied one -- both must be in the include dir the top #includes.
    assert (ROOT / "include" / "rf_samp_relay_task.h").is_file()
    assert (ROOT / "include" / "rf_samp_ingress_task.h").is_file()


def test_the_generated_top_is_two_tasks_and_a_channel():
    top = (ROOT / "gen" / f"{TOP}.cpp").read_text(encoding="utf-8")
    assert "#pragma HLS INTERFACE axis port=s_in" in top
    assert "#pragma HLS INTERFACE axis port=s_out" in top
    assert "#pragma HLS INTERFACE ap_ctrl_none port=return" in top
    # The whole fix, as four lines of generated C++: a one-block channel between two tasks.
    assert "hls_thread_local hls::stream<ap_uint<64> > blk_fifo;" in top
    assert "#pragma HLS STREAM variable=blk_fifo depth=64" in top
    assert "hls_thread_local hls::task t0(rf_samp_ingress_task<64>, s_in, blk_fifo);" in top
    assert "hls_thread_local hls::task t1(rf_samp_relay_task, blk_fifo, s_out);" in top
