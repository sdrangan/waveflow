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
import tempfile
from pathlib import Path

import numpy as np
import pytest

from examples.rf_loopback.rf_loopback_xsi import (
    TOP,
    XSI_BLKSIZE,
    XSI_NBLK,
    make_sim,
)
from waveflow.build.trace_steps import XSI_RUNNER, rtl_staleness, xsi_runner_cmd

ROOT = Path(__file__).resolve().parents[2] / "examples" / "rf_loopback"
XSI = ROOT / "xsi"
PROJ = ROOT / f"{TOP}_proj" / "solution1" / "syn" / "verilog"
#: The two-channel DUT's RTL — a separate top, so the one-channel project is untouched by it.
PROJ_2CH = ROOT / "rf_pass_through_2ch_proj" / "solution1" / "syn" / "verilog"

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
#: Data blocks that survive the round trip, of ``XSI_NBLK``.  **7 of 8** — 6 until 2026-08-31.
#:
#: The extra block is the converter model getting a cycle more honest, not this design getting
#: better: ``RfdcDacSlave`` used to judge each beat by a ``TREADY`` it had recomputed from an
#: occupancy that had already advanced, rather than by the one it actually drove a cycle earlier, so
#: it captured words a cycle early and back-pressured a cycle sooner than the wire did.  With the
#: phase corrected the DAC stalls the fabric slightly less, this read-then-write relay spends
#: slightly less time not reading, and one more block survives.  The number of *dropped* words is
#: unchanged at 62 — which is the point: the loss is still structural (see
#: :data:`WANT_ADC_DROPPED_IS_STRUCTURAL`), it just costs one block less at this depth.
WANT_DATA_BLOCKS_OUT = 7
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
#:
#: **58 -> 57 on 2026-08-31**, and it is the invariant rather than the number that is checked: the
#: DAC emits ``DAC_BLOCKS_OUT`` blocks and exactly :data:`WANT_DATA_BLOCKS_OUT` of them carry data,
#: so 64 - 7 = 57 falls straight out of the extra surviving block recorded there.
WANT_DAC_ZERO_FILLED = 57
#: Blocks of zero-fill before the first data block reaches the sink **at RTL**.
#:
#: **One, re-recorded 2026-08-31.**  The history is worth keeping because the number has now been
#: both values for opposite reasons:
#:
#: * it was **one** while ``RfdcDacSlave`` accepted every word the instant it was offered;
#: * it became **two** on 2026-08-17 when the model learned to withhold ``TREADY`` and hold itself to
#:   its own grid — a real DAC's input FIFO delays the first block by a period;
#: * it is **one** again since the model stopped judging a beat by a ``TREADY`` it had recomputed
#:   rather than by the one it drove.  That cost every capture a cycle, and a cycle at this grid is
#:   enough to push the first data block past a period boundary.
#:
#: pysim shows two, for its own reason (it paces the RF side on the edge's metronome, XSI on the
#: source), so the two backends no longer agree here — which is the divergence this note has always
#: recorded rather than reconciled, now visible again.
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
    #
    # SEVEN fields since 2026-08-22: 14 effective bits, 4 per beat, full scale 1.0, a 16-bit
    # container, justified left by 2, and the two I/Q rules.  It used to read `RfdcFormat{16, 4, 1.0}`
    # -- one width doing both jobs, which is the defect RfdcSampWord exists to fix.  The I/Q pair is
    # emitted BY NAME, so a generated harness says what was assumed rather than carrying a 0 or 1 in
    # a position that can be transposed without failing to compile.  Field ORDER is a contract here
    # (the struct is aggregate-initialized from this string) and is checked against the C++ in
    # tests/build/test_xsi_rfdc_samp.py::test_the_format_literal_rfdc_emits_reads_back_field_for_field.
    assert adc.args[0] == "RfdcFormat{14, 4, 1.0, 16, 2, RFDC_REAL, RFDC_I_LOW}"
    assert not adc.args[0].isidentifier()


def test_words_per_cycle_is_derived_not_declared():
    """``samp_rate / (samp_per_word * f_axis)`` — both terms read from the clocks that own them."""
    from examples.rf_loopback.rf_loopback_xsi import make_xsi_tb

    tb = make_xsi_tb()
    got = tb.rfdc.words_per_cycle(tb.rfdc.rx_streams[0], tb.rfdc.rx_samp_rate)
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
    # `*_proj/` is gitignored build output, and a gate that compares a cycle count against RTL it
    # did not produce reports "a real behaviour change" when the truth is a stale artifact. See
    # rtl_staleness().
    _require(rtl_staleness(ROOT, TOP) is None, rtl_staleness(ROOT, TOP) or "")

    from waveflow.build.composite_gen import render_rtl_f
    from examples.rf_loopback.rf_loopback_xsi import generate_tb

    # Regenerate the harness + scenario, and the file list from the RTL actually on disk.
    generate_tb(ROOT)
    (XSI / f"rtl_{TOP}.f").write_text(
        render_rtl_f(TOP, ROOT, stamp_sources=False), encoding="utf-8")
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
    # periods 1,3,4,5,6,8,9 with zero-fill in the gaps.  It reads a whole block before it writes one,
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

    # Startup transient: 2 blocks in pysim, 1 at RTL -- THEY DISAGREE, and that is the record.
    #
    # The cause has never gone away: pysim paces the RF side on the edge metronome and XSI on the
    # source, so the RTL ADC has its first block at t=0 and pysim's does not.  The number agreed for
    # two weeks (2026-08-17 .. 08-31) only because a SECOND offset happened to cancel it -- the
    # converter model judged each beat by a TREADY it had recomputed rather than by the one it drove,
    # which cost every capture a cycle and pushed the first data block past a period boundary.  With
    # that phase corrected the coincidence is gone and the real divergence is visible again.
    #
    # Which is why the note said "by ARITHMETIC COINCIDENCE, not by construction" while they agreed.
    # Two unrelated offsets that summed the same way, and then one of them moved.
    assert sim.tb.dac_if.counters()["underrun"] == int(sim.tb.loop_blk_latency) == 2
    assert RTL_STARTUP_BLOCKS == 1

    # Blocks emitted: pysim's grid runs for n_blk periods, the RTL grid for the whole harness run,
    # because a DAC that plays continuously does not stop when the data does.  Neither number is
    # "the answer"; the run length is a testbench constant on both sides.
    assert len(sim.captured) == XSI_NBLK
    assert len(rtl) == WANT_SINK_BLOCKS > len(sim.captured)


# ---------------------------------------------------------------------------
# The TILE at RTL — Stage A's gate (plans/adc_model.md, "Stage A — the tile")
# ---------------------------------------------------------------------------
#
# The same loopback with TWO converter channels, through a two-channel RTL top.  What needs RTL to
# state at all is the lowering: **one BFM model per direction spanning BOTH AXIS ports plus the RF
# edge**, because the edge carries every channel in one block and n_ch models cannot each own it.
# The byte-identical claim is pysim's and is gated in tests/examples/test_rf_loopback.py::TestTheTile
# — this design drops at the boundary (pattern A), so the RTL run cannot make it and does not try.

#: Words one lane's ADC produces — the same 512 the one-channel gate accounts for, per channel.
WANT_2CH_ADC_WORDS_PER_CH = XSI_NBLK * (XSI_BLKSIZE // 4)

#: Words each lane drops, and it is **62 — the one-channel number, unchanged**.
#:
#: That equality is the result, not the value.  The lanes are independent (separate ingress, separate
#: block stage, separate FIFO), so each should behave exactly as the one-channel design does; a
#: number that differed would mean they are coupled somewhere they should not be — through the
#: converter model's shared rate accumulator, most plausibly, which is the one thing they really do
#: share.  See ``WANT_ADC_DROPPED_IS_STRUCTURAL`` for why the loss exists at all and why 62 is a
#: property of the model's 2-word input FIFO rather than of silicon.
WANT_2CH_ADC_DROPPED_PER_CH = 62

#: Samples at the head of the first data block that survive, on both lanes.  **The WHOLE block since
#: 2026-08-31** — it was 12 (three words at four samples each) while the converter model captured
#: each beat a cycle early and therefore back-pressured a cycle sooner than the wire did.
#:
#: The loss did not go away and the dropped count did not move: what changed is *which* block pays
#: for it.  The first block now survives intact and the drops land in the ones after it, which is
#: also why one more block survives overall (:data:`WANT_DATA_BLOCKS_OUT`).
#:
#: It is asserted because it must be the same on both lanes, which is the claim — a lane that lost
#: differently would be a lane that is not independent.
WANT_2CH_LEAD_SAMPLES = XSI_BLKSIZE

#: Cycle the last block reached the sink — recorded the way 1072 was, and the same kind of number:
#: a measured property of THIS design at THIS rate, which changes only if the design or the rate
#: does. The one-channel run's equivalent is not this number and is not meant to be; what is
#: meaningful across the two is the block accounting below, which IS identical.
WANT_2CH_SINK_LAST_BLOCK_CYCLE = 15751


@pytest.mark.xsi
def test_the_two_channel_tile_runs_at_rtl_as_two_independent_lanes():
    """THE Stage-A RTL gate: one converter, two AXIS ports per direction, two lanes of real Verilog.

    Three claims, and only the first needs two channels to be sayable at all:

    1. **One model per direction.** ``ADC_N_CH == 2`` is the C++ model reporting how many ports it
       bound — two objects here would mean the port group did not resolve and the RF edge had two
       owners.
    2. **The lanes are independent and identical.** Every per-lane counter matches the other lane's,
       and matches the one-channel design's.
    3. **The block accounting is unchanged.** 64 grid periods, 58 zero-filled, 63 at the sink — the
       one-channel gate's constants exactly. That is the check on the DAC's block grid being derived
       from one channel's ROW rather than from the whole block: dividing by ``n_ch * blksize`` would
       have halved the grid rate and every one of these numbers would be different.
    """
    _require((XSI / XSI_RUNNER).exists(), f"{XSI / XSI_RUNNER}")
    _require(PROJ_2CH.is_dir(),
             f"no csynth RTL at {PROJ_2CH} — run rf_loopback_xsi.py's 2-channel build")

    from waveflow.build.composite_gen import render_rtl_f
    from examples.rf_loopback.rf_loopback_xsi import (
        OUT_BUNDLE_2CH,
        TOP_2CH,
        XSI_NCH,
        generate_tb_2ch,
        make_sim_2ch,
    )

    # `*_proj/` is gitignored build output, and a gate that compares a cycle count against RTL it
    # did not produce reports "a real behaviour change" when the truth is a stale artifact. See
    # rtl_staleness().  Below the import because TOP_2CH is defined in the module it names.
    _require(rtl_staleness(ROOT, TOP_2CH) is None, rtl_staleness(ROOT, TOP_2CH) or "")

    generate_tb_2ch(ROOT)
    (XSI / f"rtl_{TOP_2CH}.f").write_text(
        render_rtl_f(TOP_2CH, ROOT, stamp_sources=False), encoding="utf-8")
    shutil.rmtree(XSI / "xsim.dir" / TOP_2CH, ignore_errors=True)
    shutil.rmtree(XSI / OUT_BUNDLE_2CH, ignore_errors=True)   # never pass on last run's output

    r = subprocess.run(xsi_runner_cmd(TOP_2CH, "rf_loopback_2ch_counters"), cwd=str(XSI),
                       capture_output=True, text=True, timeout=1800)
    out = (r.stdout or "") + (r.stderr or "")
    assert "XSI_EXITCODE=0" in out, f"the 2-channel loopback did not complete cleanly:\n{out[-3000:]}"
    counters = {k: int(v) for k, v in re.findall(r"^(\w+)=(\d+)$", out, re.M)
                if k not in ("XSI_EXITCODE",)}

    # --- 1) ONE model per direction, spanning both ports ----------------------------------------
    assert counters["ADC_N_CH"] == counters["DAC_N_CH"] == XSI_NCH, (
        f"the converter models bound {counters['ADC_N_CH']}/{counters['DAC_N_CH']} AXIS ports, "
        f"expected {XSI_NCH} each. One model spans every port of its direction, because the RF edge "
        f"behind them carries every channel in one block: {counters}")

    # --- 2) The lanes are independent, and each is the one-channel design ------------------------
    for ch in range(XSI_NCH):
        sent, dropped = counters[f"ADC_WORDS_SENT_{ch}"], counters[f"ADC_DROPPED_{ch}"]
        assert sent + dropped == WANT_2CH_ADC_WORDS_PER_CH, (
            f"lane {ch} should account for every one of {WANT_2CH_ADC_WORDS_PER_CH} words: "
            f"{counters}")
        assert dropped == WANT_2CH_ADC_DROPPED_PER_CH, (
            f"lane {ch} dropped {dropped} words, the one-channel design drops "
            f"{WANT_2CH_ADC_DROPPED_PER_CH}. A DIFFERENT number means the lanes are coupled "
            f"somewhere they should not be — the converter model's rate accumulator is the one "
            f"thing they really share.")
    assert counters["ADC_DROPPED"] == XSI_NCH * WANT_2CH_ADC_DROPPED_PER_CH
    assert counters["ADC_WORDS_SENT"] == sum(counters[f"ADC_WORDS_SENT_{ch}"]
                                             for ch in range(XSI_NCH))
    assert counters["DAC_WORDS_RECV_0"] == counters["DAC_WORDS_RECV_1"]
    assert counters["DAC_UNDERRUN_0"] == counters["DAC_UNDERRUN_1"]

    # --- 3) The block accounting is the one-channel gate's, unchanged ----------------------------
    assert counters["DAC_BLOCKS_OUT"] == WANT_DAC_BLOCKS_OUT
    assert counters["DAC_BLOCKS_ZERO_FILLED"] == WANT_DAC_ZERO_FILLED
    assert counters["SINK_BLOCKS_IN"] == WANT_SINK_BLOCKS
    assert counters["ADC_CHAN_DROPPED"] == 0 and counters["DAC_CHAN_DROPPED"] == 0
    assert counters["SRC_BLOCKS_OUT"] == XSI_NBLK
    assert counters["ADC_CHAN_TRANSFERRED"] == XSI_NBLK
    assert counters["SINK_LAST_BLOCK_CYCLE"] == WANT_2CH_SINK_LAST_BLOCK_CYCLE, (
        f"the last block reached the sink at cycle {counters['SINK_LAST_BLOCK_CYCLE']}, gate "
        f"expects {WANT_2CH_SINK_LAST_BLOCK_CYCLE}. That is a real behaviour change: either a "
        f"regression or an improvement worth re-recording.")

    # --- 4) Each lane carried ITS OWN samples -----------------------------------------------------
    from waveflow.simulation.rf_tb import read_rf_bundle

    got = read_rf_bundle(XSI / OUT_BUNDLE_2CH, XSI_NCH, XSI_BLKSIZE)
    assert got, "the run dumped no RF output bundle"
    assert got[0].shape == (XSI_NCH, XSI_BLKSIZE)
    sim = make_sim_2ch()
    with tempfile.TemporaryDirectory() as scratch:
        sim.write_scenario(scratch)                 # the same writer the harness's vectors came from

    data = [b for b in got if np.any(b)]
    assert len(data) == WANT_DATA_BLOCKS_OUT, (
        f"{len(data)} of {len(got)} grid periods carried data, gate expects "
        f"{WANT_DATA_BLOCKS_OUT} — the one-channel number, for the same pattern-A reason")

    first, sent = data[0], sim.sent[0]
    n = WANT_2CH_LEAD_SAMPLES
    for ch in range(XSI_NCH):
        assert np.array_equal(first[ch][:n], sent[ch][:n]), (
            f"lane {ch}'s first {n} samples are not what was played into it")
        # `n` is the whole block now, so "where does it diverge" is "nowhere" -- and the LANES claim
        # is still the point: both must survive to exactly the same depth, because a lane that lost
        # differently would be a lane that is not independent.
        assert int(np.count_nonzero(first[ch] != sent[ch])) == 0, (
            f"lane {ch} diverges from what was played into it where lane 0 does not; the lanes drop "
            f"identically or they are not independent")
    # ...and the lanes are not crossed, which one channel cannot check at all: the two rows carry
    # different draws, so a swap would fail here even though the totals would not move.
    assert not np.array_equal(sent[0], sent[1]), "the scenario must be asymmetric to say anything"
    assert not np.array_equal(first[0], sent[1][:XSI_BLKSIZE])


# ---------------------------------------------------------------------------
# INTERLEAVED I/Q at RTL — Stage D's gate (plans/adc_model.md, "Stage D")
# ---------------------------------------------------------------------------
#
# The same five-node graph with the converter in ``iq_mode``: complex blocks on the RF side, 2
# complex samples in each 64-bit beat.
#
# **The DUT is ``rf_pass_through``, unchanged and not re-synthesized**, and that is the result rather
# than a shortcut. Complex-ness is a property of the WORD; a word is a bag of bits to the fabric. If
# an I/Q loopback had needed a different top, something would have leaked the sample geometry into
# the RTL between the converter ports.

#: Words one I/Q run produces: 8 blocks x (256 complex samples / 2 per beat).  Note it is **half**
#: the real run's 512-per-block arithmetic per sample and the same per beat — the geometry that keeps
#: a complex word at 64 bits.
WANT_IQ_ADC_WORDS = XSI_NBLK * (XSI_BLKSIZE // 2)

#: Data blocks that survive, of ``XSI_NBLK`` — 6, the same as the real run, and for the same
#: pattern-A reason: this pass-through reads a whole block before it writes one.  The rate is halved
#: for the I/Q run (see ``XSI_IQ_SAMP_RATE``) so the utilisation matches, which is why the number
#: matched the real run's — **until 2026-08-31, when it stopped**.  The converter-model phase
#: correction recorded on :data:`WANT_DATA_BLOCKS_OUT` bought the real run a seventh block and did
#: not buy this one, so the two are now 7 and 6.
#:
#: That is a finding rather than a discrepancy to reconcile: matching utilisation makes the two runs
#: cost the *same fraction* of the converter, and a cycle recovered at the boundary is worth a whole
#: block only if it lands on the right side of a grid period.  The I/Q run's periods are twice as
#: long, so the same cycle falls well inside one.  What both runs still share is the shape — the
#: first block survives whole and the drops land behind it.
WANT_IQ_DATA_BLOCKS_OUT = 6

#: Complex samples at the head of the first data block that survive.  **The WHOLE block since
#: 2026-08-31**, for the reason recorded on :data:`WANT_2CH_LEAD_SAMPLES`: the converter model used
#: to capture each beat a cycle early and back-pressure a cycle sooner than the wire did, so the
#: first block was clipped.  It was 6 — three beats at two complex samples each, the same three beats
#: the real run lost as 12.
WANT_IQ_LEAD_SAMPLES = XSI_BLKSIZE

#: Blocks the DAC's grid emits, how many carry no data, and how many reach the sink.  Half the real
#: run's 64/58/63 because the sample rate is halved and the grid is derived from it — the DAC plays
#: on its own clock, so a fixed cycle budget buys half as many block periods.
WANT_IQ_DAC_BLOCKS_OUT = 32
WANT_IQ_DAC_ZERO_FILLED = 26
WANT_IQ_SINK_BLOCKS = 31

#: Cycle the last block reached the sink — recorded the way 1072 was.
WANT_IQ_SINK_LAST_BLOCK_CYCLE = 15501


@pytest.mark.xsi
def test_interleaved_iq_runs_at_rtl_through_the_unchanged_real_dut():
    """**Stage D's RTL gate.** Complex blocks through real Verilog, both components intact.

    Four claims, and the first is the one that needed RTL to say at all:

    1. **The I/Q rules reached the C++ models.** The converter models report the format they were
       built with, so an ``iq_mode`` that quietly defaulted is visible here rather than only in the
       data. ``word_bits() == 64`` is the geometry claim: a complex word on the same bus.
    2. **The DUT is the real one-channel ``rf_pass_through``**, not re-synthesized and not a variant.
    3. **Both components survive**, checked separately against a scenario that drew them
       independently — a converter that dropped Q would return blocks of the right shape and length.
    4. **The accounting is the real run's**, at matched utilisation.
    """
    _require((XSI / XSI_RUNNER).exists(), f"{XSI / XSI_RUNNER}")
    _require(PROJ.is_dir(), f"no csynth RTL at {PROJ} — run rf_dut_build.py --through csynth")
    # `*_proj/` is gitignored build output, and a gate that compares a cycle count against RTL it
    # did not produce reports "a real behaviour change" when the truth is a stale artifact. See
    # rtl_staleness().
    _require(rtl_staleness(ROOT, TOP) is None, rtl_staleness(ROOT, TOP) or "")

    from waveflow.build.composite_gen import render_rtl_f
    from examples.rf_loopback.rf_loopback_xsi import (
        OUT_BUNDLE_IQ,
        XSI_IQ_WORD,
        generate_tb_iq,
        make_sim_iq,
    )

    generate_tb_iq(ROOT)
    (XSI / f"rtl_{TOP}.f").write_text(
        render_rtl_f(TOP, ROOT, stamp_sources=False), encoding="utf-8")
    shutil.rmtree(XSI / "xsim.dir" / TOP, ignore_errors=True)
    shutil.rmtree(XSI / OUT_BUNDLE_IQ, ignore_errors=True)   # never pass on last run's output

    r = subprocess.run(xsi_runner_cmd(TOP, "rf_loopback_iq_counters"), cwd=str(XSI),
                       capture_output=True, text=True, timeout=1800)
    out = (r.stdout or "") + (r.stderr or "")
    assert "XSI_EXITCODE=0" in out, f"the I/Q loopback did not complete cleanly:\n{out[-3000:]}"
    counters = {k: int(v) for k, v in re.findall(r"^(\w+)=(\d+)$", out, re.M)
                if k not in ("XSI_EXITCODE",)}

    # --- 1) The I/Q rules reached the models --------------------------------------------------
    assert counters["FMT_IQ_MODE"] == 1, (
        f"the converter models were built real: {counters}. An iq_mode that defaults is invisible "
        f"in the data until a Q goes missing, which is why the format is printed and asserted.")
    assert counters["FMT_IQ_ORDER"] == 0, "i_low, the declared default (see the bring-up log)"
    assert counters["FMT_SLOTS_PER_WORD"] == 4, "2 complex samples, two slots each"
    assert counters["FMT_WORD_BITS"] == int(XSI_IQ_WORD.bitwidth) == 64, (
        "the geometry claim: an I/Q design fits the same 64-bit bus by halving samp_per_word")

    # --- 2) ...through the DUT the REAL loopback already synthesized --------------------------
    assert (PROJ / f"{TOP}.v").is_file(), (
        f"this gate drives {TOP}, the one-channel real loopback's RTL. That it needs no top of its "
        f"own is the point: the fabric between two converter ports relays 64-bit beats and has no "
        f"opinion about what they carry.")

    # --- 3) Both components survive, and neither is the other ---------------------------------
    from waveflow.simulation.rf_tb import read_rf_bundle

    from waveflow.simulation.rf_tb import RF_ELEMENT_COMPLEX, RF_ELEMENT_KEY
    from waveflow.utils.burst_io import read_burst_meta

    # The manifest field, cross-language and end to end: the C++ sink wrote this bundle and Python
    # reads it back with a reader that REQUIRES the key. Before the writer emitted it a missing key
    # was read as real -- which for a complex capture is not an error, just twice as many plausible
    # samples.
    assert read_burst_meta(XSI / OUT_BUNDLE_IQ)[RF_ELEMENT_KEY] == RF_ELEMENT_COMPLEX
    got = read_rf_bundle(XSI / OUT_BUNDLE_IQ, 1, XSI_BLKSIZE, complex_samp=True)
    assert got and all(b.dtype == np.complex128 for b in got)
    assert len(got) == WANT_IQ_SINK_BLOCKS

    sim = make_sim_iq()
    with tempfile.TemporaryDirectory() as scratch:
        sim.write_scenario(scratch)              # the writer the harness's vectors came from

    data = [b for b in got if np.any(b)]
    assert len(data) == WANT_IQ_DATA_BLOCKS_OUT, (
        f"{len(data)} of {len(got)} grid periods carried data, gate expects "
        f"{WANT_IQ_DATA_BLOCKS_OUT} — the real run's number, for the same pattern-A reason")

    first, sent = data[0][0], sim.sent[0][0]
    n = WANT_IQ_LEAD_SAMPLES
    assert np.array_equal(first[:n].real, sent[:n].real), "I"
    assert np.array_equal(first[:n].imag, sent[:n].imag), "Q"
    assert np.any(sent[:n].imag), "the scenario must carry a non-trivial Q"
    assert not np.array_equal(sent[:n].real, sent[:n].imag), "I and Q must differ, or a swap hides"
    # Neither component diverges anywhere in the block, and that is a STRONGER statement than the
    # matched divergence index it replaces: a de-interleave that slipped by one would put I where Q
    # belongs at every sample, so an exact whole-block match rules it out outright.
    assert int(np.count_nonzero(first != sent)) == 0

    # --- 4) The accounting -------------------------------------------------------------------
    assert counters["ADC_WORDS_SENT"] + counters["ADC_DROPPED"] == WANT_IQ_ADC_WORDS
    assert counters["ADC_DROPPED"] > 0, (
        "this design is expected to drop -- see WANT_ADC_DROPPED_IS_STRUCTURAL. A zero would mean "
        "the converter model stopped back-pressuring, not that I/Q fixed pattern A.")
    assert counters["DAC_BLOCKS_OUT"] == WANT_IQ_DAC_BLOCKS_OUT
    assert (counters["DAC_BLOCKS_ZERO_FILLED"]
            == counters["DAC_BLOCKS_OUT"] - WANT_IQ_DATA_BLOCKS_OUT == WANT_IQ_DAC_ZERO_FILLED)
    assert counters["SINK_BLOCKS_IN"] == WANT_IQ_SINK_BLOCKS
    assert counters["ADC_CHAN_DROPPED"] == 0 and counters["DAC_CHAN_DROPPED"] == 0
    assert counters["SRC_BLOCKS_OUT"] == XSI_NBLK
    assert counters["SINK_LAST_BLOCK_CYCLE"] == WANT_IQ_SINK_LAST_BLOCK_CYCLE, (
        f"the last block reached the sink at cycle {counters['SINK_LAST_BLOCK_CYCLE']}, gate "
        f"expects {WANT_IQ_SINK_LAST_BLOCK_CYCLE}. That is a real behaviour change: either a "
        f"regression or an improvement worth re-recording.")
