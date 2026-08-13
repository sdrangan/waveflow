"""The RF loopback at RTL: source → ADC → real Verilog → DAC → sink.

``plans/adc_model.md`` staging item 2. The same five-node graph the pysim golden runs, with the
digital logic as synthesized RTL, the converters as the ``xsi_rfdc.h`` models, and the RF environment
as file-backed peers across two behavioral edges.

**What this gate establishes, and what it refuses to paper over.**

The bit-exactness claim *holds*: the first block completes the whole chain — Python quantize → pack →
AXI-Stream → real RTL → unpack → dequantize → Python — and comes back **bit-identical**. That is the
claim the arc exists to make, and it is made here for the first time end to end.

The loss claim does *not* match pysim, and the difference is a real design finding rather than a
model bug:

    the ADC produces 512 words and the fabric accepts 440.  **72 are dropped.**

``RfSampPassThrough`` reads a whole 64-word block and only then writes it, so ``TREADY`` is low for
~64 cycles at a stretch while the converter presents a beat every ~4.7 cycles regardless. A real ADC
drops; it cannot stall. pysim does not show this because its ``StreamIFMaster`` **blocks** when the
DUT is not ready — the backend asymmetry recorded in ``plans/adc_model.md``, previously unexercised
because the fabric is ~4.7x oversized *on average*. Averages are not the constraint; burstiness is.

So the gate asserts the truth: the first block is exact, and the loss is exactly the measured 72.
Pinning a known shortfall is deliberate — it makes the defect visible and regression-guarded instead
of hidden behind a tolerance. Fixing it is a design change (overlap the read and write, i.e. two
tasks and a channel, which is what ``mem_copy`` does) and is not this step.
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
WANT_ADC_DROPPED = 72
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
WANT_DAC_ZERO_FILLED = 13
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
    assert np.array_equal(got[lat], sim.sent[0]), (
        "the first DATA block is not bit-identical through the chain. That is a genuine "
        "quantization or packing disagreement between the C++ and Python converters — diagnose it, "
        "do not tolerate it.")

    # --- 2) The loss, which does NOT match pysim, and is a DESIGN finding ------------------------
    assert counters["ADC_WORDS_SENT"] + counters["ADC_DROPPED"] == WANT_ADC_WORDS, (
        f"the ADC should account for every one of {WANT_ADC_WORDS} words: {counters}")
    assert counters["ADC_DROPPED"] == WANT_ADC_DROPPED, (
        f"the ADC dropped {counters['ADC_DROPPED']} words, the recorded shortfall is "
        f"{WANT_ADC_DROPPED}. This number is pinned because it is a KNOWN DESIGN LIMIT, not a "
        f"target: RfSampPassThrough reads a whole block before writing it, so TREADY is low for "
        f"~64 cycles while the converter presents a beat every ~4.7 regardless. If it moved DOWN, "
        f"the design got better and the gate should be re-recorded; if it moved UP, something got "
        f"worse.")
    assert counters["DAC_BLOCKS_OUT"] == WANT_DAC_BLOCKS == len(got)

    # --- 2b) The DAC plays on its GRID, so it emits for the whole run ---------------------------
    assert counters["DAC_BLOCKS_OUT"] == WANT_DAC_BLOCKS_OUT == len(got)
    assert counters["DAC_BLOCKS_ZERO_FILLED"] == WANT_DAC_ZERO_FILLED, (
        f"the DAC zero-filled {counters['DAC_BLOCKS_ZERO_FILLED']} of its "
        f"{counters['DAC_BLOCKS_OUT']} grid periods, gate expects {WANT_DAC_ZERO_FILLED} "
        f"(1 startup + the tail after the 8 data blocks run out)")

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
    # pysim: whole BLOCKS on the edge.  Its ADC->fabric loss is ZERO -- not because nothing is lost
    # in hardware, but because the loss is sub-block and block-LT cannot resolve it (see
    # docs/guide/rf/fidelity.md).  XSI: 72 WORDS at the fabric boundary.
    assert sim.tb.adc_if.counters()["overrun"] == 0
    assert sim.tb.dut.s_in.interface.dropped == 0, (
        "pysim reports no ADC->fabric loss for this design; if that changes, the divergence table "
        "in plans/adc_model.md and guide/rf/fidelity.md must change with it")

    # Startup transient: 2 blocks in pysim, 1 at RTL.  Same phenomenon, different pacing -- pysim
    # paces the RF side on the edge metronome, XSI on the source.  Recorded, not averaged away.
    assert sim.tb.dac_if.counters()["underrun"] == int(sim.tb.loop_blk_latency) == 2
    assert RTL_STARTUP_BLOCKS == 1

    # Blocks emitted: pysim's grid runs for n_blk periods, the RTL grid for the whole harness run,
    # because a DAC that plays continuously does not stop when the data does.  Neither number is
    # "the answer"; the run length is a testbench constant on both sides.
    assert len(sim.captured) == XSI_NBLK
    assert len(rtl) == WANT_DAC_BLOCKS > len(sim.captured)
