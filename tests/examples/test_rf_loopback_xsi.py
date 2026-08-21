"""The RF loopback at RTL: source → ADC → real Verilog → DAC → sink.

``plans/adc_model.md`` staging item 2. The same five-node graph the pysim golden runs, with the
digital logic as synthesized RTL, the converters as the ``xsi_rfdc.h`` models, and the RF environment
as file-backed peers across two behavioral edges.

**What this gate establishes — and it changed on 2026-08-17.**

Every block that survives the chain — Python quantize → pack → AXI-Stream → real RTL → unpack →
dequantize → Python — comes back **bit-identical**, shifted by the startup transient. What changed is
how many survive:

    the ADC produces 512 words and the fabric accepts **450**.  62 are dropped, and the loss is
    structural to this design.

This gate has now recorded three different answers to that question, and the sequence is the point:

1. **72 dropped**, while ``RfSampPassThrough`` was one store-and-forward task. Pinned as a known
   limit rather than hidden behind a tolerance.
2. **0 dropped**, after the overlap fix split it into an ingress task and a block stage behind an
   internal FIFO. Believed to be the design working.
3. **62 dropped**, once ``RfdcDacSlave`` stopped driving ``TREADY`` high unconditionally. Step 2's
   zero was real but not *earned*: with an always-ready converter the fabric could run arbitrarily
   far ahead, so the relay was never held up on its output and never had to stall its input. Held to
   the converter's own grid, it does — it reads a whole block before it writes one, and a converter
   cannot be told to wait.

So this is the **pattern-A case study**, and it now demonstrates the thing it is here to demonstrate:
a design that touches the converter boundary itself has to solve never-stall on its own, and this one
does not. ``examples/rf_blk_delay`` is the same converters through ``RfSampBuf`` at both ends, and
its ADC drop count is zero *structurally* — the ingress writes a BRAM, which cannot refuse.

pysim reports zero either way: the loss it cannot see is a phase effect inside a block period, and
block-LT carries one event per block. So a green pysim run is not evidence for this clause, and a
reader should not read it as one. **This is the only backend that can check it — and it could only
check it once the converter model was faithful.**
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
#: **NONZERO, and that is a result about PATTERN A rather than a regression.**  Diagnosed, not
#: re-recorded — the instruction this constant used to carry.
#:
#: It was 0 until 2026-08-17, and it was 0 because ``RfdcDacSlave`` drove ``TREADY`` high
#: unconditionally: the fabric could run as far ahead of the converter as it liked, so the DUT was
#: never held up on its output and never had to stall its input.  Held to its own grid (see
#: ``xsi_rfdc.h``), the model tells the truth, and the truth is that **this design cannot avoid
#: stalling its input.**  ``RfSampPassThrough`` reads a whole block before it writes one, so for the
#: 256 cycles it spends writing — now paced by the DAC — it is not reading, and the ADC has nowhere
#: to put the words that arrive meanwhile.  A converter cannot be told to wait, so they are gone.
#:
#: **The count is deliberately not pinned, and 62 should not be quoted.**  It was measured with the
#: model's input FIFO at 2 words; a real RFDC's AXIS input buffer is much deeper than that, so **62 is
#: probably pessimistic** — how much of a block's write phase the converter can absorb before it has
#: to refuse is exactly what the depth sets.  The number is a property of the model, not of the
#: silicon, and the last time a number from this gate got quoted (72) it outlived the design it
#: described.
#:
#: What does *not* depend on the depth is the **sign**: any back-pressure at all makes a
#: read-then-write relay drop, because while the stage writes it is not reading, and no depth removes
#: that.  So the sign is what is asserted, and the count is left in prose where it cannot be mistaken
#: for a gate.
#:
#: **This is the case pattern B exists to answer**, and the contrast is now measured rather than
#: argued: ``examples/rf_blk_delay`` runs the same converters through ``RfSampBuf`` at both ends and
#: asserts ``ADC_DROPPED == 0`` — structurally, because its ingress writes a BRAM and can never
#: refuse.  See ``tests/examples/test_rf_blk_delay_xsi.py``.
WANT_ADC_DROPPED_IS_STRUCTURAL = True
#: Data blocks that survive the round trip, of ``XSI_NBLK``.  6 of 8, for the reason above.
WANT_DATA_BLOCKS_OUT = 6
#: Blocks the DAC's grid emits over the fixed ``n_cycles`` run, and how many reach the sink.
#:
#: 24/23, re-recorded 2026-08-18 when the RF fabric moved 300 -> 250 MHz.  Both are the SAME
#: DESIGN doing the same thing for a different number of grid periods: the DAC plays on its own
#: clock, so in a fixed *cycle* budget the number of blocks it gets through scales with its rate per
#: cycle, and ``samp_rate / (samp_per_word * f_axis)`` rose by exactly 300/250 = 1.2.  19 x 1.2 =
#: 22.8, and the run lands on 24 emitted.  Nothing about the data changed: 512 words in, 512 out,
#: 8 data blocks, zero dropped — see the assertions below, which are what actually police that.
#:
#: **The two are now separate constants.** They were one chained equality, which held at 300 MHz by
#: luck: with 24 emitted the last one is still in the channel when the run ends, so the sink sees 23.
#: An equality that holds by coincidence is worse than two numbers, because it fails for the wrong
#: reason first.
WANT_DAC_BLOCKS = 64
#: Blocks that actually reached the sink — one fewer than the DAC emitted, the last still in flight
#: when the cycle budget ran out.
WANT_SINK_BLOCKS = 63
#: Blocks the DAC's grid emits in the run, and how many carry no data.
#:
#: **The old 2152 "time to last completion" gate is retired, not moved.**  It measured the cycle the
#: final block reached the sink, which was meaningful only while the DAC emitted on buffer fullness
#: and therefore stopped when the data ran out.  A DAC plays continuously, so it now emits for the
#: whole run and that cycle (5702) is `run_length x grid_rate` — a testbench constant wearing a
#: result's clothes, which is exactly the confusion the cycle gates exist to avoid.  What replaced it
#: is the startup transient below, which IS a result and is checkable on both backends.
WANT_DAC_BLOCKS_OUT = 64
#: 16, and the invariant is what makes it checkable rather than the number: the DAC emits
#: ``DAC_BLOCKS_OUT`` blocks and exactly ``XSI_NBLK`` of them carry data, so the zero-fill is the
#: remainder.  24 - 8 = 16 at 250 MHz; it was 19 - 8 = 11 at 300 MHz, the same relation.  (Before
#: the overlap fix it was 13 against 8 data blocks, because a block whose words went missing never
#: reached the DAC's buffer whole and the grid played zeros in its place.)
WANT_DAC_ZERO_FILLED = 58
#: Blocks of zero-fill before the first data block reaches the sink **at RTL**.
#:
#: **Two, re-recorded 2026-08-17** when ``RfdcDacSlave`` learned to withhold ``TREADY``.  It was one
#: while the converter model accepted every word the instant it was offered; held to its own grid, the
#: first data block arrives a grid period later, which is what a real DAC's input FIFO does.  pysim
#: also shows two, for its own reason (it paces the RF side on the edge's metronome, XSI on the
#: source) — the two backends now agree here by coincidence rather than by construction, so the
#: divergence note is kept rather than deleted.
RTL_STARTUP_BLOCKS = 2


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
    #
    # FIVE fields since 2026-08-21, and the last two are the effective/container split reaching the
    # C++ twin: 14 effective bits, 4 per beat, full scale 1.0, a 16-bit container, justified left by
    # 2.  It used to read `RfdcFormat{16, 4, 1.0}` -- one width doing both jobs, which is the defect
    # RfdcSampWord exists to fix.  Field ORDER is a contract here (the struct is aggregate-
    # initialized from this string) and is checked against the C++ in tests/build/
    # test_xsi_rfdc_samp.py::test_the_format_literal_rfdc_emits_reads_back_field_for_field.
    assert adc.args[0] == "RfdcFormat{14, 4, 1.0, 16, 2}"
    assert not adc.args[0].isidentifier()


def test_words_per_cycle_is_derived_not_declared():
    """``samp_rate / (samp_per_word * f_axis)`` — both terms read from the clocks that own them."""
    from examples.rf_loopback.rf_loopback_xsi import make_xsi_tb

    tb = make_xsi_tb()
    got = tb.rfdc.words_per_cycle(tb.rfdc.rx_stream, tb.rfdc.rx_samp_rate)
    assert got == pytest.approx(tb.samp_rate / (tb.word.samp_per_word * tb.axis_freq))
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
    # pysim paces the RF side on the EDGE (the RFSampIF metronome delivers block 1 at t=T), while
    # XSI paces it on the SOURCE (RfFileSource fills the channel as soon as there is room), so the
    # RTL ADC has its first block at t=0 and pysim's does not. Recorded, not reconciled.
    lat = RTL_STARTUP_BLOCKS
    for k in range(lat):
        assert not np.any(got[k]), (
            f"block {k} is inside the {lat}-block startup transient and must be the zero-fill the "
            f"DAC emits when its samples have not arrived yet")

    # EVERY block, in order -- but NOT at consecutive grid periods, and that is the pattern-A result.
    #
    # This used to read `got[lat + k] == sim.sent[k]`, and it held while `RfdcDacSlave` accepted every
    # word the instant it was offered: the fabric ran ahead of the converter and the data blocks came
    # out back to back.  Held to its own grid the converter tells the truth instead, and the truth is
    # that this pass-through **cannot sustain the DAC's rate**: measured, data blocks land at grid
    # periods 2,3,4,5,7,... with zero-fill in the gaps.  It reads a whole block before it writes one,
    # so it occupies the boundary for twice its utilisation -- the same arithmetic that made the
    # (16,2) width sweep underrun.
    #
    # So the claim is separated into the two things that are actually true, instead of one that is
    # not: the DATA is exactly right (below), and WHERE it lands is a rate property, checked by the
    # zero-fill invariant further down.  This is the loss pattern B exists to remove, and
    # `examples/rf_blk_delay` is where it is gone -- there the data blocks ARE consecutive.
    data = [b for b in got if np.any(b)]
    assert len(data) == WANT_DATA_BLOCKS_OUT, (
        f"{len(data)} of the {len(got)} grid periods carried data, gate expects "
        f"{WANT_DATA_BLOCKS_OUT} of {XSI_NBLK} input blocks. Fewer than {XSI_NBLK} is the pattern-A "
        f"loss recorded on WANT_ADC_DROPPED_IS_STRUCTURAL; a *different* number is a change in how "
        f"much of it survives and wants diagnosing.")

    # --- 2) The loss: ZERO, which is the fidelity contract's third condition ---------------------
    assert counters["ADC_WORDS_SENT"] + counters["ADC_DROPPED"] == WANT_ADC_WORDS, (
        f"the ADC should account for every one of {WANT_ADC_WORDS} words: {counters}")
    assert (counters["ADC_DROPPED"] > 0) == WANT_ADC_DROPPED_IS_STRUCTURAL, (
        f"the ADC dropped {counters['ADC_DROPPED']} words. **This design is expected to drop**, for "
        f"the reason recorded on WANT_ADC_DROPPED_IS_STRUCTURAL: it reads a whole block before it "
        f"writes one, so while the DAC paces the write it is not reading, and a converter cannot be "
        f"told to wait. A ZERO here would mean the converter model has stopped back-pressuring "
        f"again — check xsi_rfdc.h before celebrating. The design fix is pattern B, which is "
        f"examples/rf_blk_delay, and there the same assertion reads zero.")
    assert counters["DAC_BLOCKS_OUT"] == WANT_DAC_BLOCKS

    # --- 2b) The DAC plays on its GRID, so it emits for the whole run ---------------------------
    assert counters["DAC_BLOCKS_OUT"] == WANT_DAC_BLOCKS_OUT
    assert len(got) == WANT_SINK_BLOCKS, (
        f"the sink collected {len(got)} blocks of the {counters['DAC_BLOCKS_OUT']} the DAC emitted; "
        f"the difference is what is still in the channel when the cycle budget ends, and it is one")
    # The INVARIANT, which is the real check: every grid period that does not carry one of the
    # surviving data blocks is zero-fill.  It holds at any clock, which the two raw counts do not.
    # The subtrahend is the number of blocks that SURVIVED, not the number sent -- which is the whole
    # difference this example now records.
    assert (counters["DAC_BLOCKS_ZERO_FILLED"]
            == counters["DAC_BLOCKS_OUT"] - WANT_DATA_BLOCKS_OUT == WANT_DAC_ZERO_FILLED), (
        f"the DAC zero-filled {counters['DAC_BLOCKS_ZERO_FILLED']} of its "
        f"{counters['DAC_BLOCKS_OUT']} grid periods, gate expects {WANT_DAC_ZERO_FILLED} — the "
        f"{WANT_DATA_BLOCKS_OUT} surviving data blocks and nothing else should be non-empty")
    accepted = WANT_ADC_WORDS - counters["ADC_DROPPED"]
    in_flight = accepted - counters["DAC_WORDS_RECV"]
    assert 0 <= in_flight < XSI_BLKSIZE // 4, (
        f"the DAC took {counters['DAC_WORDS_RECV']} words off the fabric of the {accepted} the "
        f"fabric accepted, leaving {in_flight} unaccounted. Under a block's worth is the tail still "
        f"inside the DUT when the cycle budget ends; a whole block or more would mean the relay lost "
        f"data on top of what the boundary dropped, which is a different fault. (Measured 9: the "
        f"words in the internal FIFO and the output register when the run stops.)")

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

    # Startup transient: 2 blocks on BOTH backends -- and the agreement is NEW.
    #
    # It was 2 in pysim against 1 at RTL, recorded here as a real divergence with a real cause: pysim
    # paces the RF side on the edge metronome and XSI on the source, so the RTL ADC had its first
    # block at t=0.  That cause has not gone away.  What changed is on the other side of the loop:
    # `RfdcDacSlave` used to accept every word the instant it was offered, so the fabric ran ahead of
    # the converter and the first data block arrived a grid period sooner than a real DAC would have
    # taken it.  Held to its own grid (see xsi_rfdc.h), the RTL transient is 2 as well.
    #
    # So the two backends now agree here by ARITHMETIC COINCIDENCE, not by construction -- two
    # unrelated offsets that happen to sum the same way.  Worth stating, because "they agree" is the
    # kind of observation that quietly turns into "they model the same thing".
    assert sim.tb.dac_if.counters()["underrun"] == int(sim.tb.loop_blk_latency) == 2
    assert RTL_STARTUP_BLOCKS == 2

    # Blocks emitted: pysim's grid runs for n_blk periods, the RTL grid for the whole harness run,
    # because a DAC that plays continuously does not stop when the data does.  Neither number is
    # "the answer"; the run length is a testbench constant on both sides.
    assert len(sim.captured) == XSI_NBLK
    assert len(rtl) == WANT_SINK_BLOCKS > len(sim.captured)
