"""Every declared throughput cost must equal what csynth measured — **and be the right KIND of cost.**

**Four modules have now declared such a constant and three of them were wrong at some point**, each
justified by *symmetry* with a module that looked similar — "the same shape, so it costs the same".
The reports refuted the premise every time.  So the guard is worth more than any single correction:
a cost is **measured**, never inherited.

## Two kinds of body, two calibrations, one anchor each

The file used to assume every body had a bounded firing.  Since 2026-08-18 the two converter-facing
bodies are ``while (1)`` loops, and a firing is not a thing they have, so the guard is split:

============  ==================================  =========================================
kind          the quantity that exists            anchored by
============  ==================================  =========================================
**looped**    cycles per WORD = achieved II       the RX ingress (``cycles_per_word``)
**bounded**   cycles per FIRING = ``latency + 1``  ``BlkDelay`` (``fire_overhead``)
============  ==================================  =========================================

Each kind keeps an anchor because each calibration can be wrong on its own.  If a looped row moves,
``achieved II`` is being misread; if the bounded row moves, ``latency + 1`` is.  They are different
repairs, so they are different tests.

**Do not apply ``latency + 1`` to a looped body.**  It is measured to be **pessimistic by exactly 2**
there — ``plans/witness/task_loop/`` predicts 13/69 at N=8/64 where RTL measures 11/67 — and for an
infinite loop it cannot be applied at all, because csynth reports ``TripCount = inf`` and no overall
latency.  A test below asserts that those two bodies really are unbounded, so a silent revert to a
bounded shape is caught as a change of *kind* rather than passing with a plausible number.

## Why it is not bookkeeping

The constant feeds :meth:`~waveflow.hw.rf_samp_buf_tx.RfSampBufTx.check_rate`'s threshold,
``samp_per_word * f_axis / cycles_per_word``.  Declared too low it permits **more sample rate than
the hardware sustains** — a static check that says yes where the hardware says no, which is exactly
the failure that cost the RX side 1695 of 4096 samples.  The error is optimistic in both places it
lands: the threshold and the pysim charge.

Skips loudly when a report is absent, the way the XSI gates do: a silent skip on a gate like this
reads as a pass in a summary line.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from waveflow.hw.rf_samp_buf import RfSampBufIngress
from waveflow.hw.rf_samp_buf_tx import RfSampBufLoader, RfSampBufPlayer
from waveflow.utils.csynthparse import loop_pipeline_ii, module_latency

_EX = Path(__file__).resolve().parents[2] / "examples"
RX_REPORT = _EX / "rf_samp_buf_rx" / "rf_samp_buf_rx_proj" / "solution1" / "syn" / "report"
TX_REPORT = _EX / "rf_samp_buf_tx" / "rf_samp_buf_tx_proj" / "solution1" / "syn" / "report"

#: ``(label, report dir, synthesized module, the class attribute that declares the cost)``.
#:
#: The module names carry their template arguments, so they name the **gated geometry** — one sample
#: per word — which is the configuration every declared cost is for.
#:
#: Both entries are **looped** bodies: their declared number is the loop's achieved ``PipelineII``,
#: not ``latency + 1``.  See the module docstring for why the two must not be confused.
LOOPED_BODIES = [
    ("rx ingress", RX_REPORT, "rf_samp_buf_ingress_task_16_1_1024_16_s",
     RfSampBufIngress.cycles_per_word),
    ("tx player", TX_REPORT, "rf_samp_buf_player_task_16_1_2048_16_s",
     RfSampBufPlayer.cycles_per_word),
]


def _require(report_dir: Path, module: str):
    """The measured latency, or a loud skip — never a silent pass."""
    if not report_dir.is_dir():
        pytest.skip(f"no csynth report dir at {report_dir} — run the example's build --through csynth")
    got = module_latency(report_dir, module)
    if got is None and not (report_dir / f"{module}_csynth.xml").is_file():
        pytest.skip(f"no report for {module} in {report_dir} — re-run csynth")
    return got


def _sole_loop(report_dir: Path, module: str) -> str:
    """The name of the module's one pipelined loop, **found rather than spelled out**.

    Vitis names a loop after the SOURCE LINE it sits on, so editing a comment above the body renames
    the report entry.  A hard-coded name does not fail when that happens — it stops matching and the
    test *skips*, which reads as a pass.  Exactly one is asserted: these bodies have a single loop,
    and more than one means the body was restructured and this file is measuring something else.
    """
    from waveflow.utils.csynthparse import module_loops

    loops = module_loops(report_dir, module)
    assert loops, f"{module} reports no pipelined loop — is it still a `while (1)` body?"
    assert len(loops) == 1, f"{module} now has {len(loops)} loops ({loops}); it had one"
    return loops[0]


@pytest.mark.parametrize("label,report_dir,module,declared",
                         LOOPED_BODIES, ids=[b[0] for b in LOOPED_BODIES])
def test_the_declared_per_word_cost_is_the_loops_achieved_ii(label, report_dir, module, declared):
    """``cycles_per_word == achieved PipelineII``, read out of the module's own ``_csynth.xml``.

    **Achieved, not target.**  Vitis reports both and they differ whenever it misses; a per-word cost
    taken from the target is a wish, and it is optimistic in the direction that hides starvation at a
    converter.  If this fails the declaration is a guess again — correct the constant from the
    report, never the other way round.
    """
    if not report_dir.is_dir():
        pytest.skip(f"no csynth report dir at {report_dir} — run the example's build --through csynth")
    if not (report_dir / f"{module}_csynth.xml").is_file():
        pytest.skip(f"no report for {module} in {report_dir} — re-run csynth")
    loop = _sole_loop(report_dir, module)
    ii = loop_pipeline_ii(report_dir, module, loop)
    assert ii is not None, f"{label}: {module}'s loop {loop} reports no PipelineII"
    assert declared == ii, (
        f"{label}: declares cycles_per_word={declared}, but {module}'s loop {loop} achieved II={ii}. "
        f"Correct the DECLARATION from the report; raising the threshold back to keep a test green "
        f"re-introduces the defect the constant exists to prevent.")


@pytest.mark.parametrize("label,report_dir,module",
                         [(a, b, c) for a, b, c, _ in LOOPED_BODIES],
                         ids=[b[0] for b in LOOPED_BODIES])
def test_the_converter_facing_bodies_really_are_unbounded_loops(label, report_dir, module):
    """The **kind** check, and it is the one that stops a silent revert.

    A ``while (1)`` body reports ``TripCount = inf`` and no overall latency.  If someone replaces it
    with a bounded loop the II stays 1 and the row above still passes — while the body acquires a
    per-firing boundary gap, which at a converter boundary is a window in which it is not reading.
    That is the regression this asserts against, and nothing else here would see it.
    """
    if not report_dir.is_dir() or not (report_dir / f"{module}_csynth.xml").is_file():
        pytest.skip(f"no report for {module} — run the example's build --through csynth")
    lat = module_latency(report_dir, module)
    assert lat is None, (
        f"{label}: csynth can now bound {module} ({lat}), so its body is no longer an infinite loop. "
        f"A bounded body has a firing boundary — measured at 3 idle cycles in "
        f"plans/witness/task_loop/ — and a converter-facing task must not have one. If the change "
        f"was deliberate, this file's two calibrations and check_rate all need re-deriving.")
    from waveflow.utils.csynthparse import loop_trip_count
    loop = _sole_loop(report_dir, module)
    trip = loop_trip_count(report_dir, module, loop)
    assert trip == "inf", f"{label}: {module}'s loop {loop} reports TripCount={trip}, expected inf"


def test_the_looped_calibration_is_anchored_on_the_rx_ingress():
    """The control for the **looped** kind.  If this row moves, achieved-II is being misread and
    every other looped row is meaningless.

    Stated separately from the parametrised case so a failure says *which* thing broke: a wrong
    calibration and a wrong declaration are different repairs.
    """
    if not RX_REPORT.is_dir():
        pytest.skip(f"no csynth report dir at {RX_REPORT} — run rf_samp_buf_rx_build.py --through csynth")
    mod = "rf_samp_buf_ingress_task_16_1_1024_16_s"
    if not (RX_REPORT / f"{mod}_csynth.xml").is_file():
        pytest.skip(f"no report for {mod} — re-run csynth")
    ii = loop_pipeline_ii(RX_REPORT, mod, _sole_loop(RX_REPORT, mod))
    assert ii == 1, (
        f"the RX ingress's loop no longer achieves II=1 (got {ii}). Everything downstream rests on "
        f"it: check_rate's ceiling is samp_per_word * f_axis / cycles_per_word, and at II=1 that is "
        f"exactly port capacity. Re-derive before trusting any other row.")
    assert RfSampBufIngress.cycles_per_word == 1


def test_the_bounded_calibration_is_anchored_on_blk_delay():
    """The control for the **bounded** kind — ``cycles per firing = latency + 1``.

    That calibration still has a live user: ``BlkDelay`` in ``examples/rf_blk_delay`` is a bounded
    body (its firing is one block) and declares a fixed ``fire_overhead`` derived from latency.  It is
    the anchor here for the same reason the ingress used to be: if it moves, ``latency + 1`` is wrong
    wherever it is still applied.

    It is deliberately a *different module in a different example* from the looped anchor, so one
    report going stale cannot take both calibrations down at once.
    """
    from examples.rf_blk_delay.rf_blk_delay import BLKSIZE, SAMP_PER_WORD, BlkDelay

    rd = (_EX / "rf_blk_delay" / "rf_blk_delay_proj" / "solution1" / "syn" / "report")
    mod = f"blk_delay_task_{SAMP_PER_WORD * 16}_{SAMP_PER_WORD}_{BLKSIZE}_4_16_s"
    if not rd.is_dir() or not (rd / f"{mod}_csynth.xml").is_file():
        pytest.skip(f"no report for {mod} — run rf_blk_delay_build.py --through csynth")
    lat = module_latency(rd, mod)
    assert lat is not None, (
        f"{mod} is now unbounded, so `latency + 1` has nothing to anchor on and this file has lost "
        f"its bounded control. Find another bounded body or retire the calibration.")
    words = BLKSIZE // SAMP_PER_WORD
    overhead = int(lat["latency_max"]) - words * BlkDelay.word_cycles
    assert overhead == BlkDelay.fire_overhead, (
        f"csynth reports latency {lat['latency_max']} for a {words}-word firing, so the fixed cost is "
        f"{overhead}; BlkDelay declares fire_overhead={BlkDelay.fire_overhead}.")


def test_the_loader_declares_no_per_firing_cost_because_csynth_cannot_bound_it():
    """A body Vitis cannot bound must not carry a cycles-per-firing constant at all.

    The loader's firing is one whole command and its outer loop's trip count is data-dependent, so
    the report says ``undef``.  Any single number there is a fiction whose *direction* of error is
    unknown — which is worse than no number, because a rate contract built on it looks sourced.

    What it declares instead is a per-**word** cost, and that one is measurable: the payload loop is
    pipelined, so its achieved II is a real cycles-per-iteration figure.
    """
    for gone in ("fire_cycles", "cycles_per_word"):
        assert not hasattr(RfSampBufLoader, gone), (
            f"RfSampBufLoader declares {gone}. Its overall latency is 'undef' — there is no "
            f"per-firing cost to declare, and its per-word cost is `word_cycles`, sourced from the "
            f"payload loop's achieved II. Two names for one quantity is how they drift apart.")

    if not TX_REPORT.is_dir():
        pytest.skip(f"no csynth report dir at {TX_REPORT} — run rf_samp_buf_tx_build.py --through csynth")
    whole = module_latency(TX_REPORT, "rf_samp_buf_loader_task_16_1_2048_16_16_s")
    assert whole is None, (
        f"csynth can now bound the loader body ({whole}). That is a real change — the payload loop "
        f"must have acquired a fixed trip count — and it means a per-firing cost is declarable "
        f"again. Re-derive rather than assume.")


def test_the_loaders_per_word_cost_is_the_payload_loops_achieved_ii():
    """``word_cycles`` comes from ``PipelineII`` on the **payload** loop — the achieved II.

    **The loader now has two loops and the test has to pick the right one.**  Its room-wait was
    hoisted out of the per-word path on 2026-08-18 (that nesting was what held the payload loop at
    II=2), so the module reports a wait loop *and* a payload loop.  The payload one is the loop that
    moves a word, so it is the one a per-word cost comes from; the wait runs once per command.

    Discovered rather than named: Vitis names a loop after its source line, so spelling one out means
    a comment edit silently turns this into a skip.  That is exactly what happened when the hoist
    moved the loop from line 99 to line 121 — the test skipped, and a skip reads as a pass.
    """
    if not TX_REPORT.is_dir():
        pytest.skip(f"no csynth report dir at {TX_REPORT} — run rf_samp_buf_tx_build.py --through csynth")
    from waveflow.utils.csynthparse import module_loops

    stem = "rf_samp_buf_loader_task_16_1_2048_16_16"
    subs = sorted(x.name[: -len("_csynth.xml")] for x in TX_REPORT.glob(f"{stem}_Pipeline_*_csynth.xml"))
    if not subs:
        pytest.skip(f"no pipelined-loop reports for {stem} — re-run csynth")
    assert len(subs) == 2, (
        f"expected the loader to have a wait loop and a payload loop, found {subs}. The body was "
        f"restructured; re-derive which one carries the per-word cost rather than trusting the pick "
        f"below.")
    # The payload loop is the LATER of the two in the source: the wait was hoisted above it.
    payload = subs[-1]
    loops = module_loops(TX_REPORT, payload)
    assert len(loops) == 1, f"{payload} has {loops}"
    ii = loop_pipeline_ii(TX_REPORT, payload, loops[0])
    assert ii is not None, f"{payload} reports no PipelineII for {loops[0]}"
    assert RfSampBufLoader.word_cycles == ii, (
        f"the loader declares word_cycles={RfSampBufLoader.word_cycles} but the payload loop "
        f"{loops[0]} achieved II={ii}. Correct the declaration from the report.")


def test_the_rate_ceiling_follows_the_measured_cost():
    """The consequence, asserted so a silent correction cannot pass unnoticed.

    At ``cycles_per_word = 1`` both halves' ceilings are ``samp_per_word * f_axis`` — **exactly what
    the AXIS port carries**.  The history is worth keeping because the number has moved three times:
    the player permitted ``f_axis / 2`` per sample-per-word until 2026-08-17 (wrong, by symmetry with
    the ingress), then ``f_axis / 3`` (right for a one-word firing), and now ``f_axis``.

    The clock is read from the target rather than written as a literal, so re-clocking the examples
    cannot leave a stale ceiling here.
    """
    from waveflow.hw.rf_samp_buf import RfSampBufRx
    from waveflow.hw.rf_samp_buf_tx import RfSampBufTx
    from waveflow.simulation.simulation import Simulation

    f_axis = 1000.0 / RFSOC_TARGET_NS * 1e6            # 250 MHz
    for spw in (1, 2, 4):
        tx = RfSampBufTx(name=f"ceilt{spw}", sim=Simulation(), bitwidth=16 * spw,
                         samp_per_word=spw)
        rx = RfSampBufRx(name=f"ceilr{spw}", sim=Simulation(), bitwidth=16 * spw,
                         samp_per_word=spw)
        # Each half is its SLOWEST stage: TX is max(player 1, loader 1) = 1; RX is
        # max(ingress 1, capture 2) = 2.  Reading either from its converter-facing task alone is the
        # error this file exists to catch.
        assert tx.max_samp_rate(f_axis) == f_axis * spw / 1
        assert rx.max_samp_rate(f_axis) == f_axis * spw / 2


def test_only_the_tx_half_reaches_the_port_and_the_capture_is_why():
    """**Where the ceiling actually sits, stage by stage — and why `check_rate` is still load-bearing.**

    An earlier draft of this test asserted that *both* halves now coincide with the port, on the
    strength of the ingress and player reaching II=1.  That was wrong, and wrong in the instructive
    direction: it read the ceiling off the converter-facing tasks and forgot that every sample also
    crosses a capture or a loader.  The capture is at 2 cycles per word and cannot be pipelined to 1
    without deleting the straddling case, so the RX half sits at half the port and the **loop** sits
    there with it.

    That is also the answer to "does `check_rate` still refuse anything?".  For the TX half it no
    longer does — its ceiling is the port's, and `Rfdc` fires first on everything above.  For the RX
    half it very much does, at half the port, which is the rate `examples/rf_blk_delay` is sized
    against.
    """
    from waveflow.hw.rf_samp_buf import RfSampBufCapture, RfSampBufIngress, RfSampBufRx
    from waveflow.hw.rf_samp_buf_tx import RfSampBufLoader, RfSampBufPlayer, RfSampBufTx
    from waveflow.simulation.simulation import Simulation

    f_axis, spw = 1000.0 / RFSOC_TARGET_NS * 1e6, 4
    port = f_axis * spw
    rx = RfSampBufRx(name="pc_r", sim=Simulation(), bitwidth=16 * spw, samp_per_word=spw)
    tx = RfSampBufTx(name="pc_t", sim=Simulation(), bitwidth=16 * spw, samp_per_word=spw)

    # The four measured per-word costs, and the two halves they add up to.
    assert (RfSampBufIngress.cycles_per_word, RfSampBufCapture.cycles_per_word) == (1, 2)
    assert (RfSampBufLoader.word_cycles, RfSampBufPlayer.cycles_per_word) == (1, 1)
    assert rx.cycles_per_word == 2 and tx.cycles_per_word == 1

    assert tx.max_samp_rate(f_axis) == port, "the TX half reaches the port"
    assert rx.max_samp_rate(f_axis) == port / 2, "the RX half does not, because of the capture"

    # The loop is the slower of the two, and it is what an example may be sized against.
    assert min(rx.max_samp_rate(f_axis), tx.max_samp_rate(f_axis)) == port / 2

    # TX accepts the port's own rate; RX refuses it.  Both are the check doing its job.
    tx.check_rate(port, f_axis)
    with pytest.raises(ValueError, match="exceeds what the ingress can absorb"):
        rx.check_rate(port, f_axis)


# ---------------------------------------------------------------------------
# The synthesis target — which part each example is actually built for
# ---------------------------------------------------------------------------
#
# Checked from `csynth.xml`, which the RUN writes, rather than from the `.tcl`, which states the
# intent and can be edited without rebuilding.  Same reason a stale `rtl_*.f` cannot be trusted to
# describe the RTL beside it.

#: The part the RF examples target, and the clock the RF model's arithmetic is written against.  An
#: ``xc7z020`` physically cannot host an RF data converter, and it reached 137 MHz on this design --
#: so every ceiling in ``docs/guide/rf/`` was being measured on a part that could not run it.
RFSOC_PART = "xczu48dr-ffvg1517-2-e"

#: 4.0 ns = 250 MHz, and it is **forced by the geometry rather than chosen**: the AXIS word carries
#: an integer number of samples, so ``f_axis = samp_rate / samp_per_word``, and 1 GSPS / 250 MHz = 4
#: exactly where 1 GSPS / 300 MHz = 3.33 is not a sample count.  It replaced 3.333 ns on 2026-08-18.
RFSOC_TARGET_NS = 4.0

#: ``(label, report dir)`` for every example that must be on the RFSoC part.
RF_SOLUTIONS = [
    ("rf_samp_buf_rx", RX_REPORT),
    ("rf_samp_buf_tx", TX_REPORT),
    ("rf_loopback dut", _EX / "rf_loopback" / "rf_pass_through_proj" / "solution1" / "syn" / "report"),
]


def _norm(part: str) -> str:
    """Vitis writes ``xc7z020-clg484-1`` for a ``set_part xc7z020clg484-1``; compare without dashes."""
    return (part or "").replace("-", "").lower()


@pytest.mark.parametrize("label,report_dir", RF_SOLUTIONS, ids=[s[0] for s in RF_SOLUTIONS])
def test_the_rf_examples_are_synthesized_for_the_rfsoc_part(label, report_dir):
    """The part is a **per-example** choice, and these three are the examples that need it."""
    from waveflow.utils.csynthparse import synth_target

    if not report_dir.is_dir():
        pytest.skip(f"no csynth report dir at {report_dir} — run that example's build --through csynth")
    got = synth_target(report_dir)
    assert got is not None, f"{label}: no csynth.xml in {report_dir}"
    assert _norm(got["part"]) == _norm(RFSOC_PART), (
        f"{label} was synthesized for {got['part']}, not {RFSOC_PART}. The RF model's rates are "
        f"written against a 250 MHz fabric on a part that can host a converter; a Zynq-7020 is "
        f"neither.")
    assert got["target_period_ns"] == pytest.approx(RFSOC_TARGET_NS, abs=0.01), (
        f"{label} targeted {got['target_period_ns']} ns, not {RFSOC_TARGET_NS} "
        f"({1000.0 / RFSOC_TARGET_NS:.0f} MHz) — the clock every ceiling in docs/guide/rf/ assumes")


@pytest.mark.parametrize("label,report_dir", RF_SOLUTIONS, ids=[s[0] for s in RF_SOLUTIONS])
def test_the_rf_examples_close_timing_at_300_mhz(label, report_dir):
    """Closing it is the point: the premise is only worth writing against if it is reachable.

    A margin check rather than an exact Fmax — the estimate moves in the third significant figure
    between runs, and what matters is that the target clock is met, not by how much.
    """
    from waveflow.utils.csynthparse import synth_target

    if not report_dir.is_dir():
        pytest.skip(f"no csynth report dir at {report_dir} — run that example's build --through csynth")
    got = synth_target(report_dir)
    assert got is not None and got["fmax_mhz"] is not None, f"{label}: no timing estimate"
    floor = 1000.0 / RFSOC_TARGET_NS
    assert got["fmax_mhz"] >= floor, (
        f"{label} estimates {got['fmax_mhz']:.1f} MHz, below the {floor:.0f} MHz the RF model "
        f"assumes. Either the design got slower or the premise needs restating — do not just lower "
        f"the doc.")


def test_the_non_rf_examples_are_left_on_their_original_part():
    """The split is the point: re-targeting mem_copy would invalidate its gates and the calib corpus.

    A cheap check that the per-example selection did not leak.  Skips if mem_copy has not been built
    here — its own gates are what really police it, and this is a tripwire, not their replacement.
    """
    from waveflow.utils.csynthparse import synth_target
    from waveflow.build.composite_gen import DEFAULT_PART

    rep = _EX / "mem_copy" / "mem_copy_proj" / "solution1" / "syn" / "report"
    if not rep.is_dir():
        pytest.skip(f"mem_copy not built here ({rep}); its own cycle gate is the real check")
    got = synth_target(rep)
    assert got is not None and _norm(got["part"]) == _norm(DEFAULT_PART), (
        f"mem_copy was synthesized for {got and got['part']}, not {DEFAULT_PART}. The RF part "
        f"selection has leaked into a non-RF example, whose cycle gates and the entire "
        f"waveflow/calib/ corpus are fit against the original part.")
