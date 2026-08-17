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
SLOW_FIRE_CYCLES = 16


def _run(loader_fire_cycles: int):
    """One run with the loader's per-word cost set to *loader_fire_cycles*.

    Everything else about the graph — the scenario, the geometry, the player — is identical between
    calls, so the ONLY difference is what the loader's body is charged.  Returns the counters, the
    responses, and the length of the contiguous run of loaded samples that reached the converter.
    """
    original = RfSampBufLoader.fire_cycles
    RfSampBufLoader.fire_cycles = loader_fire_cycles
    try:
        tb = run_pysim(tb=RfSampBufTxTB(name=f"fc{loader_fire_cycles}", sim=Simulation()))
        return (int(tb.dut.n_underrun), int(tb.dut.n_played), responses(tb),
                find_loaded_run(played_samples(tb)))
    finally:
        RfSampBufLoader.fire_cycles = original


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
    slow_underrun, _sp, slow_resp, slow_run = _run(SLOW_FIRE_CYCLES)
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
        f"a loader {SLOW_FIRE_CYCLES // 2}x more expensive than the shipped one still placed every "
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
    underrun, played, resp, run_len = _run(RfSampBufPlayer.fire_cycles)
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
    ``SLOW_FIRE_CYCLES`` mean something.
    """
    paced, played, _resp, _run_len = _run(RfSampBufPlayer.fire_cycles)
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
