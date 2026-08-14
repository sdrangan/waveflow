"""The RF loopback at RTL: source → ADC → real Verilog → DAC → sink.

``plans/adc_model.md`` staging item 2. The same five-node graph the pysim golden runs, with the
digital logic as synthesized RTL, the converters as the ``xsi_rfdc.h`` models, and the RF environment
as file-backed peers across two behavioral edges.

**What this gate establishes.**

Every one of the eight blocks completes the whole chain — Python quantize → pack → AXI-Stream → real
RTL → unpack → dequantize → Python — and comes back **bit-identical**, shifted by the one-block
startup transient. Not the first block: all of them. That was unreachable until the DUT stopped
stalling its input.

    the ADC produces 512 words and the fabric accepts **512**.  Nothing is dropped.

This gate used to pin a shortfall of 72 as a known design limit, and the pin was the right call at
the time — it made the defect visible and regression-guarded instead of hidden behind a tolerance.
The defect is now fixed, so what is guarded has flipped: ``ADC_DROPPED == 0`` is the mechanical form
of the fidelity contract's third condition (*the DUT never stalls its input* — see
``docs/guide/rf/fidelity.md``), and it is checked here because **this is the only backend that can
check it**. pysim reports zero either way: the loss it cannot see is a phase effect inside a block
period, and block-LT carries one event per block. So a green pysim run is not evidence for this
clause, and a reader should not read it as one.

What made zero reachable is the design change in ``RfSampPassThrough``: an ingress task that relays
one word at a time into an internal FIFO, and a block stage behind it that is *allowed* to be busy.
The converter now meets a port that is drained every cycle rather than in 64-cycle gulps.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

from examples.rf_loopback.rf_loopback_xsi import (
    TOP,
    XSI_BLKSIZE,
    XSI_NBLK,
    make_sim,
)
from waveflow.build.trace_steps import XSI_RUNNER, xsi_runner_cmd

ROOT = Path(__file__).resolve().parents[2] / "examples" / "rf_loopback"
XSI = ROOT / "xsi"
PROJ = ROOT / f"{TOP}_proj" / "solution1" / "syn" / "verilog"

#: What the ADC produces, and what the fabric actually takes.  8 blocks x 64 words.
WANT_ADC_WORDS = XSI_NBLK * (XSI_BLKSIZE // 4)
#: **Zero, and asserted as a contract rather than recorded as a measurement.**  Unlike the cycle
#: gates below, this number is not "what the design happens to do": a converter cannot be
#: back-pressured, so any nonzero value is lost signal.  It was 72 while the DUT was a single
#: store-and-forward task; the overlap fix (an ingress task + a one-block internal FIFO) is what
#: makes zero achievable.  Do not re-record this one — diagnose it.
WANT_ADC_DROPPED = 0
WANT_DAC_BLOCKS = 19
#: Blocks the DAC's grid emits in the run, and how many carry no data.
#:
#: **The old 2152 "time to last completion" gate is retired, not moved.**  It measured the cycle the
#: final block reached the sink, which was meaningful only while the DAC emitted on buffer fullness
#: and therefore stopped when the data ran out.  A DAC plays continuously, so it now emits for the
#: whole run and that cycle (5702) is `run_length x grid_rate` — a testbench constant wearing a
#: result's clothes, which is exactly the confusion the cycle gates exist to avoid.  What replaced it
#: is the startup transient below, which IS a result and is checkable on both backends.
WANT_DAC_BLOCKS_OUT = 19
#: 11, where the store-and-forward design zero-filled 13.  The two blocks it gained are the direct
#: consequence of the ADC no longer dropping words: a block whose words went missing never reached
#: the DAC's buffer as a whole block, so the grid played zeros in its place.
WANT_DAC_ZERO_FILLED = 11
#: Blocks of zero-fill before the first data block reaches the sink **at RTL**.  One, where pysim
#: shows two: pysim paces the RF side on the edge's metronome and XSI on the source, so the RTL ADC
#: has its first block at t=0.  A divergence to record, not to average away.
RTL_STARTUP_BLOCKS = 1


def _require(cond: bool, why: str) -> None:
    if not cond:
        pytest.skip(f"XSI gate prerequisite missing: {why}")


def _golden():
    """The pysim scenario, recomputed from its single writer."""
    import tempfile

    sim = make_sim()
    with tempfile.TemporaryDirectory() as scratch:
        sim.write_scenario(scratch)
    return sim


# ---------------------------------------------------------------------------
# Generation — no toolchain
# ---------------------------------------------------------------------------

def test_the_loopback_graph_lowers_to_a_harness():
    """The graph that could not be walked at all before this step.

    It was blocked by ``RFSampIF`` declaring no ``xsi_model()`` — not by the DUT, whose boundary
    derived all along.
    """
    from waveflow.build.composite_gen import tb_top_spec
    from examples.rf_loopback.rf_loopback_xsi import make_xsi_tb

    spec = tb_top_spec(make_xsi_tb())
    assert spec.top_name == TOP
    assert {c.cls for c in spec.channels} == {"BlockChannel<RfBlockMsg>"}
    assert len(spec.channels) == 2, "one behavioral edge per RF direction"


def test_the_converter_is_two_models_each_spanning_the_cut():
    """The first real consumer of per-port ``BfmModel`` resolution.

    One object per data path, binding RTL pins on the fabric side and a channel on the RF side —
    and the two paths take *different* classes, which one declaration per module could not express.
    """
    from waveflow.build.composite_gen import tb_top_spec
    from examples.rf_loopback.rf_loopback_xsi import make_xsi_tb

    by = {m.cls: m for m in tb_top_spec(make_xsi_tb()).models}
    adc, dac = by["RfdcAdcMaster"], by["RfdcDacSlave"]
    assert adc.binds == ("sim.dut()", f"{TOP}_ports::s_in", "xsi_tb_adc_if")
    assert dac.binds == ("sim.dut()", f"{TOP}_ports::s_out", "xsi_tb_dac_if")
    # RfdcFormat is a LITERAL, never a bare identifier: an identifier would be promoted to a
    # Harness(...) parameter typed const std::vector<uint64_t>&, which an RfdcFormat is not.
    assert adc.args[0] == "RfdcFormat{16, 4, 1.0}"
    assert not adc.args[0].isidentifier()


def test_words_per_cycle_is_derived_not_declared():
    """``samp_rate / (samp_per_word * f_axis)`` — both terms read from the clocks that own them."""
    from examples.rf_loopback.rf_loopback_xsi import make_xsi_tb

    tb = make_xsi_tb()
    got = tb.rfdc.words_per_cycle(tb.rfdc.rx_stream, tb.rfdc.rx_samp_rate)
    assert got == pytest.approx(tb.samp_rate / (tb.samp_per_word * tb.axis_freq))
    assert 0.0 < got < 1.0, "a ratio above 1 would mean the port cannot carry the rate"


def test_the_rf_peers_carry_their_bundles_as_dynparams():
    """Bundle I/O lives on the NODES; the edge has no file machinery."""
    from waveflow.build.composite_gen import tb_top_spec
    from examples.rf_loopback.rf_loopback_xsi import make_xsi_tb

    spec = tb_top_spec(make_xsi_tb())
    by = {m.cls: m for m in spec.models}
    assert by["RfFileSource"].dyn_params == (("in_bundle", '"vectors/rf_in"'),)
    assert by["RfFileSink"].dyn_params == (("out_bundle", '"vectors/rf_out"'),)
    assert all(c.dyn_params == () for c in spec.channels), "a channel carries no file config"


# ---------------------------------------------------------------------------
# The RTL run
# ---------------------------------------------------------------------------

@pytest.mark.xsi
def test_rtl_loopback_first_block_is_bit_exact_and_the_loss_is_the_measured_one():
    """THE gate. Runs the counter main, which also dumps the sink's bundle in post_sim."""
    _require((XSI / XSI_RUNNER).exists(), f"{XSI / XSI_RUNNER}")
    _require(PROJ.is_dir(), f"no csynth RTL at {PROJ} — run rf_dut_build.py --through csynth")

    from waveflow.build.composite_gen import render_rtl_f
    from examples.rf_loopback.rf_loopback_xsi import generate_tb

    # Regenerate the harness + scenario, and the file list from the RTL actually on disk.
    generate_tb(ROOT)
    (XSI / f"rtl_{TOP}.f").write_text(render_rtl_f(TOP, ROOT), encoding="utf-8")
    shutil.rmtree(XSI / "xsim.dir" / TOP, ignore_errors=True)
    shutil.rmtree(XSI / "vectors" / "rf_out", ignore_errors=True)   # never pass on last run's output

    r = subprocess.run(xsi_runner_cmd(TOP, "rf_loopback_counters"), cwd=str(XSI),
                       capture_output=True, text=True, timeout=1800)
    out = (r.stdout or "") + (r.stderr or "")
    assert "XSI_EXITCODE=0" in out, f"the RF loopback did not complete cleanly:\n{out[-3000:]}"

    counters = {k: int(v) for k, v in re.findall(r"^(\w+)=(\d+)$", out, re.M)
                if k not in ("XSI_EXITCODE",)}

    # --- 1) The bit-exactness claim, which HOLDS ------------------------------------------------
    from waveflow.simulation.rf_tb import read_rf_bundle

    got = read_rf_bundle(XSI / "vectors" / "rf_out", 1, XSI_BLKSIZE)
    sim = _golden()
    assert got, "the run dumped no RF output bundle"
    # The startup transient is real at RTL now that the DAC emits on its grid: its first period
    # comes due before the pipeline has delivered anything, so a zero block goes out.
    # The RTL transient is ONE block, where pysim's is two -- a real divergence, not a tolerance.
    # pysim paces the RF side on the EDGE (the RFSampIF metronome delivers block 1 at t=T), while
    # XSI paces it on the SOURCE (RfFileSource fills the channel as soon as there is room), so the
    # RTL ADC has its first block at t=0 and pysim's does not. Recorded, not reconciled.
    lat = RTL_STARTUP_BLOCKS
    for k in range(lat):
        assert not np.any(got[k]), (
            f"block {k} is inside the {lat}-block startup transient and must be the zero-fill the "
            f"DAC emits when its samples have not arrived yet")
    # EVERY block, not just the first.  While the DUT stalled its input, blocks after the first were
    # missing words and could only be compared loosely or not at all; a design that does not drop
    # makes the whole run checkable, so it is checked.
    for k in range(XSI_NBLK):
        assert np.array_equal(got[lat + k], sim.sent[k]), (
            f"DAC block {lat + k} != ADC block {k} through the chain. With the ADC dropping nothing, "
            f"every block must survive quantize -> pack -> RTL -> unpack -> dequantize unchanged; a "
            f"difference here is a genuine disagreement between the C++ and Python converters, or a "
            f"relay that lost data. Diagnose it, do not tolerate it.")

    # --- 2) The loss: ZERO, which is the fidelity contract's third condition ---------------------
    assert counters["ADC_WORDS_SENT"] + counters["ADC_DROPPED"] == WANT_ADC_WORDS, (
        f"the ADC should account for every one of {WANT_ADC_WORDS} words: {counters}")
    assert counters["ADC_DROPPED"] == WANT_ADC_DROPPED == 0, (
        f"the ADC dropped {counters['ADC_DROPPED']} words. A converter cannot be back-pressured, so "
        f"a dropped word is lost signal, not a slower run — this is the mechanical form of the "
        f"fidelity contract's 'the DUT never stalls its input' (docs/guide/rf/fidelity.md) and the "
        f"only backend that can check it. Nonzero means some stage stopped reading its input long "
        f"enough for a beat to go unanswered: find which, do not re-record the number. (It was 72 "
        f"while RfSampPassThrough was one store-and-forward task.)")
    assert counters["DAC_BLOCKS_OUT"] == WANT_DAC_BLOCKS == len(got)

    # --- 2b) The DAC plays on its GRID, so it emits for the whole run ---------------------------
    assert counters["DAC_BLOCKS_OUT"] == WANT_DAC_BLOCKS_OUT == len(got)
    assert counters["DAC_BLOCKS_ZERO_FILLED"] == WANT_DAC_ZERO_FILLED, (
        f"the DAC zero-filled {counters['DAC_BLOCKS_ZERO_FILLED']} of its "
        f"{counters['DAC_BLOCKS_OUT']} grid periods, gate expects {WANT_DAC_ZERO_FILLED} "
        f"(1 startup + the {WANT_DAC_BLOCKS_OUT - XSI_NBLK - RTL_STARTUP_BLOCKS} tail periods after "
        f"the 8 data blocks run out)")
    assert counters["DAC_WORDS_RECV"] == WANT_ADC_WORDS, (
        f"the DAC took {counters['DAC_WORDS_RECV']} words off the fabric of the "
        f"{WANT_ADC_WORDS} the ADC produced — with nothing dropped at either boundary these must "
        f"agree, and a shortfall here would mean the relay, not the converter, lost data")

    # --- 3) The edges themselves lose nothing: the loss is at the fabric boundary ---------------
    assert counters["ADC_CHAN_DROPPED"] == 0 and counters["DAC_CHAN_DROPPED"] == 0, (
        f"a behavioral edge dropped a block; the loss should be at the AXIS boundary only: "
        f"{counters}")
    assert counters["SRC_BLOCKS_OUT"] == XSI_NBLK
    assert counters["ADC_CHAN_TRANSFERRED"] == XSI_NBLK


@pytest.mark.xsi
def test_the_two_backends_disagree_about_loss_and_this_records_how():
    """Not a correctness check — the **input to** ``behavioral_edges.md`` S4.

    Same scenario, same graph, two backends, and they count different things in different units.
    Written down rather than reconciled: redefining either side's counters to make them line up
    would destroy exactly the information S4 needs.
    """
    _require((XSI / "vectors" / "rf_out").is_dir(),
             "no RF output bundle — run the gate above first")

    sim = make_sim()
    sim.run()
    sim.check()                       # pysim is clean: 8 blocks, 1 declared startup underrun

    from waveflow.simulation.rf_tb import read_rf_bundle
    rtl = read_rf_bundle(XSI / "vectors" / "rf_out", 1, XSI_BLKSIZE)

    # --- what each side counts, for one scenario ------------------------------------------------
    # Both now report zero ADC->fabric loss -- and the agreement is WEAKER THAN IT LOOKS, which is
    # the thing worth writing down.  pysim reported zero for the broken design too: the loss was
    # sub-block, and block-LT carries one event per block (docs/guide/rf/fidelity.md).  So pysim's
    # zero is uninformative about this clause in both directions, and only XSI's is evidence.
    assert sim.tb.adc_if.counters()["overrun"] == 0
    assert sim.tb.dut.s_in.interface.dropped == 0, (
        "pysim reports no ADC->fabric loss; note this was ALSO true of the store-and-forward design "
        "that lost 72 words at RTL, so it is not evidence that the design is correct")

    # Startup transient: 2 blocks in pysim, 1 at RTL.  Same phenomenon, different pacing -- pysim
    # paces the RF side on the edge metronome, XSI on the source.  Recorded, not averaged away.
    assert sim.tb.dac_if.counters()["underrun"] == int(sim.tb.loop_blk_latency) == 2
    assert RTL_STARTUP_BLOCKS == 1

    # Blocks emitted: pysim's grid runs for n_blk periods, the RTL grid for the whole harness run,
    # because a DAC that plays continuously does not stop when the data does.  Neither number is
    # "the answer"; the run length is a testbench constant on both sides.
    assert len(sim.captured) == XSI_NBLK
    assert len(rtl) == WANT_DAC_BLOCKS > len(sim.captured)
