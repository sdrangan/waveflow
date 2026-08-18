"""Does the TX buffer's pysim twin model the RATE, and may its body legally exist?

The two halves of ``plans/pipelined_ops.md``, pointed at the TX side.

**1. Is the twin honest?**  PR #160 established the rule for RX: a body that relays a whole burst and
charges nothing for it is silently rate-blind, and the fix is ``timeout(nwords * cycles_per_word *
period)``.  The same rule applies here and the failure it hides is the opposite one — a loader that
cannot keep up starves the player, which then plays stale slots.  The demonstration below is the
acceptance criterion: on one graph, with a loader whose body costs more than the shipped one, the
paced twin reports the loss and a zero-cost twin reports a clean window.

**2. May the body legally be what it is?**  A leaf whose ``kernel_task()`` names a hand-written
header is never extracted, so its ``run_iter`` may use constructs ``extract_kernel`` rejects.  That
exemption is a property of a code path rather than a stated rule, so it is pinned here for the TX
bodies exactly as ``test_rf_samp_buf_twin.py`` pins it for the RX ones.
"""
from __future__ import annotations

import pytest

from examples.rf_samp_buf_tx.rf_samp_buf_tx import (
    RF_SAMP_BUF_TOO_LATE,
    XSI_BLKSIZE,
    RfSampBufTxTB,
    expected_responses,
    find_loaded_run,
    played_samples,
    responses,
    run_pysim,
)
from waveflow.build.composite_gen import RFSOC4X2_CLK_HZ, composite_top_spec
from waveflow.build.elaborate import elaborate
from waveflow.hw.rf_samp_buf_tx import RfSampBufLoader, RfSampBufPlayer, RfSampBufTx
from waveflow.simulation.simulation import Simulation

#: The elaboration the RTL gate uses — one sample per word.
_ELAB = {"bitwidth": 16, "samp_per_word": 1, "depth": 1024, "horizon_margin": 16}

#: Samples of the primed window the comparison is made over.
WINDOW = 1024

#: A loader whose body costs **eight times** the shipped one — an unpipelined or much wider host
#: path.  It is the fault this file exists to make visible: the loader falls behind the free-running
#: player, which then plays slots it never filled.
#:
#: Eight rather than four because the buffer is 2048 words deep and the scenario primes four blocks
#: ahead, so the design has real margin — which is the point of sizing it that way.  The knee is
#: between 8 and 16; 16 is the first cost at which commands start coming back TOO_LATE.
SLOW_WORD_CYCLES = 16


def _run(loader_word_cycles: int):
    """One run with the loader's per-word cost set to *loader_word_cycles*.

    Everything else about the graph — the scenario, the geometry, the player — is identical between
    calls, so the ONLY difference is what the loader's body is charged.  Returns the counters, the
    responses, and the length of the contiguous run of loaded samples that reached the converter.

    ``enforce_rate=False`` **and it is required now, which is itself the news.**  Since 2026-08-18 the
    buffer's ceiling is the max over its stages rather than the player's alone, so charging the loader
    16 cycles a word drops the declared ceiling to 15.6 MSa/s and ``check_rate`` refuses the 64 MSa/s
    scenario before it can be built.  That is the check working: the whole point of this probe is a
    design that cannot keep up, and the check now sees the stage this probe cripples.  Bypassing it
    here is deliberate and local; nothing shipped does so.
    """
    original = RfSampBufLoader.word_cycles
    RfSampBufLoader.word_cycles = loader_word_cycles
    try:
        tb = run_pysim(tb=RfSampBufTxTB(name=f"fc{loader_word_cycles}", sim=Simulation(),
                                        enforce_rate=False))
        return (int(tb.dut.n_underrun), int(tb.dut.n_played), responses(tb),
                find_loaded_run(played_samples(tb)))
    finally:
        RfSampBufLoader.word_cycles = original


# ---------------------------------------------------------------------------
# 1. The twin models the rate — the acceptance criterion
# ---------------------------------------------------------------------------

def test_the_paced_twin_sees_a_loader_that_cannot_keep_up():
    """**The demonstration.**  Same graph, same scenario, same player; only the loader's cost differs.

    ``cycles_per_word = 0`` is the burst-granular predecessor exactly — the body is unchanged, it simply
    charges nothing, which is what "relay the payload and pay for it never" means.  It reports a
    clean window for a loader that in fact cannot keep up.  Charging the cost makes the player's
    starvation visible as both a wrong window and a higher underrun count.

    One line of difference rather than a re-implementation, so the comparison cannot be an artefact
    of two bodies that differ in some other way as well.
    """
    slow_underrun, _sp, slow_resp, slow_run = _run(SLOW_WORD_CYCLES)
    blind_underrun, _bp, blind_resp, blind_run = _run(0)

    # The blind twin stores the whole payload the instant it arrives, so the player never overtakes
    # it: it reports exactly the scenario's DESIGNED outcome (tid 3 late, the rest placed in full) --
    # a clean bill of health for a loader that in fact cannot keep up.
    assert blind_resp == expected_responses(1), (
        f"the zero-cost twin is supposed to be blind here and reported {blind_resp}; if it now sees "
        f"the starvation this test no longer demonstrates anything and the reason needs finding")

    # The paced twin has the player overtake the loader mid-frame, which is exactly what a starved
    # playout buffer does: the slots it was still filling have already gone out of the DAC.
    designed_late = {t for t, s, _n in expected_responses(1) if s == RF_SAMP_BUF_TOO_LATE}
    late = [t for t, s, _n in slow_resp if s == RF_SAMP_BUF_TOO_LATE and t not in designed_late]
    assert late, (
        f"a loader {SLOW_WORD_CYCLES // 2}x more expensive than the shipped one still placed every "
        f"command ({slow_resp}), so the twin is not modelling the loader's rate at all")
    assert slow_underrun > blind_underrun, (
        f"the paced twin reports {slow_underrun} underruns and the blind one {blind_underrun}; "
        f"pacing must make starvation MORE visible, not less")
    # Compared on samples PLACED rather than on the contiguous run: the run-finder measures what
    # reached the converter unbroken, which saturates at one block once the priming window shifts,
    # and a saturated measure cannot discriminate.  What the loader actually managed is `nloaded`.
    slow_placed = sum(n for _t, _s, n in slow_resp)
    blind_placed = sum(n for _t, _s, n in blind_resp)
    assert slow_placed < blind_placed, (
        f"the paced twin placed {slow_placed} samples and the blind one {blind_placed}; a starved "
        f"loader must place FEWER, not more")


def test_the_shipped_cost_plays_the_window_correctly():
    """The pacing must not invent starvation in a design that fits.

    At the measured ``cycles_per_word = 2`` the loader keeps ahead of the player and the primed window is
    bit-exact — so the fault above is the loader's cost, not the model's.
    """
    underrun, played, resp, run_len = _run(RfSampBufLoader.word_cycles)
    assert resp == expected_responses(1), (
        f"the shipped loader cost cannot place the scenario's commands: {resp}")
    assert run_len >= XSI_BLKSIZE, (
        f"only {run_len} loaded samples reached the converter contiguously at the shipped cost")
    assert 0 < underrun < played, (
        f"underrun={underrun} of {played}: the priming transient is structural and must be visible, "
        f"but it must not be everything")


def test_the_underrun_counter_does_not_measure_the_loaders_cost_while_the_loader_has_margin():
    """**A correction, and the corrected claim is the weaker one.**

    An earlier revision of this file asserted that at 250 MHz the paced twin reports MORE underruns
    than the zero-cost one — 871 against 763 — and read that as the loader charge finally becoming
    visible.  It is not.  Sweeping the loader's cost over 0, 1, 2 and 4 cycles per word gives
    underrun 1821, 1691, 1647, 1612: **monotonically decreasing as the loader gets slower**, which no
    rate story explains.  Over those same four runs the loader placed the identical 1030 samples with
    identical responses, and the player fired the identical 3328 times, so neither what was stored nor
    when it was played differed at all.

    The mechanism is :attr:`~waveflow.hw.rf_samp_buf_tx.RfSampBufPlayer.last_wr`.  It is a *lower
    bound* on the fill level, advanced only when a non-blocking poll of a **depth-1** channel happens
    to find a notice.  A fast loader emits its notices in a burst, they overwrite each other before
    the player polls, and the bound lags further behind — measured, fill notices actually seen went
    128, 257, 257, 259 across the sweep, and the final bound 1763, 1893, 1937, 1972.  The counter is
    tracking how the notices interleave with the polls, and over-reporting when they interleave badly.
    Which is exactly what that attribute's docstring already says it does; what was new was reading
    the variation as signal.

    So PR #161's original conclusion stands: this counter has no discriminating power for the
    loader's cost while the loader has margin.  What does discriminate is samples PLACED, and it moves
    only once the loader genuinely fails to keep up — see
    ``test_the_paced_twin_sees_a_loader_that_cannot_keep_up``, which is the acceptance criterion.
    """
    paced, played, resp, _run_len = _run(RfSampBufLoader.word_cycles)
    blind, _bp, bresp, _brun = _run(0)

    # 1. The loader had margin at both costs: same samples, same slots, same responses.
    assert resp == bresp == expected_responses(1), (
        f"the loader no longer has margin at one of these costs ({resp} vs {bresp}), so this test's "
        f"premise is gone and the counter may well be measuring something real now")

    # 2. And the counter moved anyway — which is the whole point.  A counter that varies while
    #    nothing it purports to measure varies is not measuring that thing.
    assert abs(paced - blind) > 0.01 * played, (
        f"paced={paced} and zero-cost={blind} of {played} now agree to within 1%. That is a BETTER "
        f"state than the one this test documents: it would mean the fill channel's sampling artefact "
        f"is gone (a deeper channel, or a blocking notice). If so, delete this test and re-derive "
        f"whether the loader charge is visible — do not widen the tolerance to keep it passing.")


# ---------------------------------------------------------------------------
# 2. The codegen exemption, pinned
# ---------------------------------------------------------------------------

def test_the_composite_generates_even_though_the_leaf_bodies_would_not_extract():
    """**The pin.**  The generator must keep never extracting a hooked leaf's ``run_iter``.

    Both halves together, because either alone is uninformative: ``composite_top_spec`` — the real
    generator — succeeds, and ``extract_kernel`` raises on the same leaves, so the exemption is
    load-bearing rather than incidental.

    ``check(leaf, "composite_kernel")`` is deliberately NOT used as evidence: it returns ``True`` for
    an unambiguously illegal body, because that gate never runs extraction for this combination.
    """
    from waveflow.build.hwcodegen import extract_kernel

    comp = elaborate(RfSampBufTx, dict(_ELAB), name="rf_samp_buf_tx")
    spec = composite_top_spec(comp, width=16)
    assert spec.top_name == "rf_samp_buf_tx"
    fns = [t.task_fn for t in spec.tasks]
    for task in ("rf_samp_buf_loader_task", "rf_samp_buf_player_task"):
        assert any(task in f for f in fns), f"{task} is not in the generated top: {fns}"

    for cls in (RfSampBufLoader, RfSampBufPlayer):
        leaf = cls(name=f"leaf_{cls.__name__}", sim=Simulation(), bitwidth=16, samp_per_word=1,
                   depth=1024)
        with pytest.raises(Exception) as err:
            extract_kernel(leaf)
        assert "SynthesisError" in type(err.value).__name__, (
            f"extract_kernel now accepts {cls.__name__}'s body ({type(err.value).__name__}). If "
            f"that is deliberate the exemption is no longer load-bearing and this test should be "
            f"rewritten, not deleted.")


def test_the_two_progress_channels_are_declared_depth_one_and_point_opposite_ways():
    """The structure that makes TX different from RX, asserted rather than described.

    RX has one channel, writer to reader.  TX needs both directions: the player tells the loader
    where it has played, and the loader tells the player how far it has filled.  Depth 1 because each
    carries a running position and only the newest value means anything.
    """
    tx = RfSampBufTx(name="chan", sim=Simulation(), **_ELAB)
    depths = {n: i.depth for n, i in tx.interfaces.items()}
    assert set(depths.values()) == {1}, f"a progress channel is not depth 1: {depths}"
    assert len(depths) == 2, f"expected exactly two progress channels, got {sorted(depths)}"
    # The endpoints prove the direction: the loader writes wr_out and reads rd_in, the player the
    # mirror.  A single channel, or two pointing the same way, would not type-check here.
    assert tx.loader.wr_out.interface is tx.player.wr_in.interface
    assert tx.player.rd_out.interface is tx.loader.rd_in.interface


# ---------------------------------------------------------------------------
# 3. What the player's cycles_per_word is worth in pysim — the replacement criterion
# ---------------------------------------------------------------------------
#
# THE ORIGINAL CRITERION FOR THIS STAGE HAD NO DISCRIMINATING POWER, and the measurement above is
# why: `test_the_underrun_counter_does_not_measure_the_loaders_cost_while_the_loader_has_margin`
# shows the underrun count varies with how the depth-1 fill notices interleave with the player's
# polls rather than with the loader's cost, because in the gate scenario the loader has margin and
# the player's starvation is driven by its own metronome outrunning it.  Charging the loader more
# only matters once the loader is the bottleneck (`SLOW_WORD_CYCLES`), which the test above covers.
#
# The PLAYER's `cycles_per_word` needs a different probe, and it took a measurement to find one.  The
# player's firing costs `max(fabric, DAC demand)`, and those two cross at exactly
# `f_axis / cycles_per_word` -- the declared ceiling.  So:
#
#   * BELOW the ceiling the DAC term dominates and `cycles_per_word` is INERT: measured identical
#     sustained rates at 64 and 100 MSa/s for cycles_per_word 2, 3 and 4.
#   * ABOVE it the fabric term dominates and the constant is what limits the player.
#
# Every LEGAL configuration is below the ceiling, because `check_rate` refuses the rest.  So the
# constant is observable in pysim only where the design is already refused -- which is the honest
# statement of what it is worth, and the reason the probe below has to disable `enforce_rate`.


def _sustained_word_rate(cycles_per_word: int, samp_rate: float) -> float:
    """Words per second the player actually sustained over a run, at a given cost and DAC demand."""
    original = RfSampBufPlayer.cycles_per_word
    RfSampBufPlayer.cycles_per_word = cycles_per_word
    try:
        tb = run_pysim(tb=RfSampBufTxTB(name=f"sus{cycles_per_word}", sim=Simulation(),
                                        samp_rate=samp_rate, enforce_rate=False))
        return int(tb.dut.n_played) / float(tb.sim.env.now)
    finally:
        RfSampBufPlayer.cycles_per_word = original


def test_below_the_ceiling_the_players_cost_is_inert():
    """**Half of the replacement criterion**, and the half that explains the other.

    At a DAC rate the player can sustain, its firing is paced by the converter and not by the fabric,
    so ``cycles_per_word`` changes nothing at all.  That is correct behaviour, not a missing effect — but
    it means no legal configuration can be used to check the constant, because ``check_rate`` refuses
    everything above the ceiling.
    """
    # Which costs are actually *below* the ceiling is a property of the clock, so it is derived:
    # the player is DAC-paced only while `f_axis / cycles_per_word >= samp_rate`.  At 250 MHz and a
    # 64 MSa/s DAC that is cycles_per_word 2 and 3; at 4 the ceiling is 62.5 MSa/s and the FABRIC binds,
    # which is the other test's territory.  Hard-coding the sweep would have quietly started
    # measuring the wrong regime when the clock moved.
    dac_rate = 64e6
    below = [fc for fc in (2, 3, 4, 5) if RFSOC4X2_CLK_HZ / fc >= dac_rate]
    assert len(below) >= 2, f"no room to compare: only {below} sit below the ceiling"
    rates = [_sustained_word_rate(fc, dac_rate) for fc in below]
    assert all(r == pytest.approx(rates[0]) for r in rates), (
        f"the player's cost changed its sustained rate below the ceiling (cycles_per_word {below} -> "
        f"{rates}); it should be DAC-paced there, and if it is not, `max(fabric, demand)` has "
        f"stopped being the model")


def test_above_the_ceiling_the_measured_cost_predicts_a_lower_rate_than_the_old_one():
    """**The discriminating half.**  In the band between the two ceilings the constants disagree, and
    the simulation follows the measured one.

    100 MSa/s (``f_axis / 3``, measured) up to 150 (``f_axis / 2``, the inherited value) is exactly
    the band where the old declaration said *yes* and the hardware said *no*.  Run there with the
    rate check disabled, the player paced by the fabric: the corrected cost sustains strictly less,
    which is the whole content of the correction.

    ``enforce_rate=False`` is required and is the point -- a configuration in this band is refused by
    the shipped design, so this probe is deliberately looking at something the design would not
    allow.
    """
    for rate in (120e6, 140e6):
        old = _sustained_word_rate(2, rate)
        measured = _sustained_word_rate(3, rate)
        assert measured < old, (
            f"at {rate/1e6:g} MSa/s the measured cost (3) sustained {measured/1e6:.1f} MSa/s and the "
            f"inherited one (2) {old/1e6:.1f}; a costlier firing must sustain LESS, or cycles_per_word "
            f"is not reaching the model at all")


def test_the_ceiling_is_where_the_two_pacing_terms_cross():
    """Why the criterion has to look above the ceiling: that is where the crossover is.

    The firing costs ``max(nwords * cycles_per_word / f_axis, nwords / word_rate)``.  Those are equal at
    ``word_rate == f_axis / cycles_per_word``, which is exactly what ``max_samp_rate`` returns — so the
    constant's whole observable effect starts at the boundary ``check_rate`` refuses beyond.
    """
    from waveflow.hw.rf_samp_buf_tx import RfSampBufTx

    dut = RfSampBufTx(name="cross", sim=Simulation(), **_ELAB)
    f_axis = RFSOC4X2_CLK_HZ
    assert dut.max_samp_rate(f_axis) == f_axis / RfSampBufPlayer.cycles_per_word
    nwords = 256
    at_ceiling = dut.max_samp_rate(f_axis)
    fabric = nwords * RfSampBufPlayer.cycles_per_word / f_axis
    demand = nwords / at_ceiling
    assert fabric == pytest.approx(demand), (
        "the fabric and DAC pacing terms no longer cross at max_samp_rate, so the ceiling and the "
        "simulation have stopped meaning the same thing")
