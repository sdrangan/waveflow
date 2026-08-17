"""Does the TX buffer's pysim twin model the RATE, and may its body legally exist?

The two halves of ``plans/pipelined_ops.md``, pointed at the TX side.

**1. Is the twin honest?**  PR #160 established the rule for RX: a body that relays a whole burst and
charges nothing for it is silently rate-blind, and the fix is ``timeout(nwords * fire_cycles *
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

import numpy as np
import pytest

from examples.rf_samp_buf_tx.rf_samp_buf_tx import (
    RF_SAMP_BUF_OK,
    RF_SAMP_BUF_TOO_LATE,
    XSI_BLKSIZE,
    RfSampBufTxTB,
    expected_responses,
    find_loaded_run,
    played_samples,
    responses,
    run_pysim,
)
from waveflow.build.composite_gen import composite_top_spec
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
    """
    original = RfSampBufLoader.word_cycles
    RfSampBufLoader.word_cycles = loader_word_cycles
    try:
        tb = run_pysim(tb=RfSampBufTxTB(name=f"fc{loader_word_cycles}", sim=Simulation()))
        return (int(tb.dut.n_underrun), int(tb.dut.n_played), responses(tb),
                find_loaded_run(played_samples(tb)))
    finally:
        RfSampBufLoader.word_cycles = original


# ---------------------------------------------------------------------------
# 1. The twin models the rate — the acceptance criterion
# ---------------------------------------------------------------------------

def test_the_paced_twin_sees_a_loader_that_cannot_keep_up():
    """**The demonstration.**  Same graph, same scenario, same player; only the loader's cost differs.

    ``fire_cycles = 0`` is the burst-granular predecessor exactly — the body is unchanged, it simply
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
    assert slow_run < blind_run, (
        f"the paced twin got {slow_run} loaded samples out contiguously and the blind one "
        f"{blind_run}; a starved player must play FEWER of them, not more")


def test_the_shipped_cost_plays_the_window_correctly():
    """The pacing must not invent starvation in a design that fits.

    At the measured ``fire_cycles = 2`` the loader keeps ahead of the player and the primed window is
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


def test_the_underrun_counter_is_the_same_at_the_shipped_cost_and_at_zero():
    """The structural transient is NOT the fault, and this separates the two.

    Before the first command's slot the buffer is genuinely empty and after the last one it is
    genuinely stale, so a correctly-fed design still reports underruns.  At the shipped cost the
    paced and zero-cost twins agree exactly, which is what makes the disagreement at
    ``SLOW_WORD_CYCLES`` mean something.
    """
    paced, played, _resp, _run_len = _run(RfSampBufLoader.word_cycles)
    blind, _bp, _bresp, _brun = _run(0)
    assert abs(paced - blind) < 0.01 * played, (
        f"paced={paced}, zero-cost={blind} of {played}: at a cost the design can afford the two must "
        f"agree to within noise, or the pacing is charging for something that is not there")


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
# 3. What the player's fire_cycles is worth in pysim — the replacement criterion
# ---------------------------------------------------------------------------
#
# THE ORIGINAL CRITERION FOR THIS STAGE HAD NO DISCRIMINATING POWER, and the measurement above is
# why: `test_the_underrun_counter_is_the_same_at_the_shipped_cost_and_at_zero` shows the paced and
# zero-cost LOADER give the same underrun count, because in the gate scenario the loader has margin
# and the player's starvation is driven by its own metronome outrunning it.  Charging the loader
# more only matters once the loader is the bottleneck (`SLOW_WORD_CYCLES`), which the test above
# covers.
#
# The PLAYER's `fire_cycles` needs a different probe, and it took a measurement to find one.  The
# player's firing costs `max(fabric, DAC demand)`, and those two cross at exactly
# `f_axis / fire_cycles` -- the declared ceiling.  So:
#
#   * BELOW the ceiling the DAC term dominates and `fire_cycles` is INERT: measured identical
#     sustained rates at 64 and 100 MSa/s for fire_cycles 2, 3 and 4.
#   * ABOVE it the fabric term dominates and the constant is what limits the player.
#
# Every LEGAL configuration is below the ceiling, because `check_rate` refuses the rest.  So the
# constant is observable in pysim only where the design is already refused -- which is the honest
# statement of what it is worth, and the reason the probe below has to disable `enforce_rate`.


def _sustained_word_rate(fire_cycles: int, samp_rate: float) -> float:
    """Words per second the player actually sustained over a run, at a given cost and DAC demand."""
    original = RfSampBufPlayer.fire_cycles
    RfSampBufPlayer.fire_cycles = fire_cycles
    try:
        tb = run_pysim(tb=RfSampBufTxTB(name=f"sus{fire_cycles}", sim=Simulation(),
                                        samp_rate=samp_rate, enforce_rate=False))
        return int(tb.dut.n_played) / float(tb.sim.env.now)
    finally:
        RfSampBufPlayer.fire_cycles = original


def test_below_the_ceiling_the_players_cost_is_inert():
    """**Half of the replacement criterion**, and the half that explains the other.

    At a DAC rate the player can sustain, its firing is paced by the converter and not by the fabric,
    so ``fire_cycles`` changes nothing at all.  That is correct behaviour, not a missing effect — but
    it means no legal configuration can be used to check the constant, because ``check_rate`` refuses
    everything above the ceiling.
    """
    rates = [_sustained_word_rate(fc, 64e6) for fc in (2, 3, 4)]
    assert rates[0] == pytest.approx(rates[1]) == pytest.approx(rates[2]), (
        f"the player's cost changed its sustained rate below the ceiling ({rates}); it should be "
        f"DAC-paced there, and if it is not, `max(fabric, demand)` has stopped being the model")


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
            f"inherited one (2) {old/1e6:.1f}; a costlier firing must sustain LESS, or fire_cycles "
            f"is not reaching the model at all")


def test_the_ceiling_is_where_the_two_pacing_terms_cross():
    """Why the criterion has to look above the ceiling: that is where the crossover is.

    The firing costs ``max(nwords * fire_cycles / f_axis, nwords / word_rate)``.  Those are equal at
    ``word_rate == f_axis / fire_cycles``, which is exactly what ``max_samp_rate`` returns — so the
    constant's whole observable effect starts at the boundary ``check_rate`` refuses beyond.
    """
    from waveflow.hw.rf_samp_buf_tx import RfSampBufTx

    dut = RfSampBufTx(name="cross", sim=Simulation(), **_ELAB)
    f_axis = 300e6
    assert dut.max_samp_rate(f_axis) == f_axis / RfSampBufPlayer.fire_cycles
    nwords = 256
    at_ceiling = dut.max_samp_rate(f_axis)
    fabric = nwords * RfSampBufPlayer.fire_cycles / f_axis
    demand = nwords / at_ceiling
    assert fabric == pytest.approx(demand), (
        "the fabric and DAC pacing terms no longer cross at max_samp_rate, so the ceiling and the "
        "simulation have stopped meaning the same thing")
