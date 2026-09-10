"""The pair, closed through a converter — ``plans/rf_shot_absolute.md`` S3.

**The headline: the channel delay is an address difference.** A sample the transmitter sent from
``mem[j]`` arrives at ``mem[(j + D) mod depth]``, so ``D`` is read off a window header and one sample
value — no timestamps, no correlator. These gates assert that reading, its aliasing, and the two ways
it can be moved.

**Toolchain-free, and not ``xsi``-marked.** What is on trial here is a claim about the *pair* in the
loosely-timed model, and both halves are already synthesized and RTL-gated by their own examples at
this very ``absolute_index = 1``: ``test_rf_shot_tx_abs_xsi.py`` (17 gates) and
``test_rf_shot_rx_abs_xsi.py`` (12). Re-deriving the address correspondence through RTL would need a
second locked memory inside one kernel and a C++ twin for the path's delay, and would restate a
number two green gate sets already stand behind. See ``plans/rf_shot_absolute.md`` S3, *What S3
built*, for that decision and what it would take to change it.

**This does not re-gate S1 or S2.** Every assertion below needs *both* ends: a transmitter phase
gate cannot see a receiver's address, and neither can see the path between them.

The two controls
----------------
* :func:`test_a_relative_pair_reads_no_single_delay_at_all` — the same graph at
  ``absolute_index = 0``. Without it the reading could be a property of the loopback rather than of
  the mode, and every gate here would still pass.
* :func:`test_the_first_waveform_is_lost_when_the_loads_are_too_close` — the scenario's spacing,
  shown by removing it. Under absolute indexing a load arriving inside the deferral window is
  cancelled **and answered ``SHOT_LOADED`` anyway**, so nothing but the capture can tell you.
"""
from __future__ import annotations

import numpy as np
import pytest

from examples.rf_shot_loopback.rf_shot_loopback import (
    ALIAS_DELAY_SAMP,
    BLKSIZE,
    CODE_A,
    DELAY_SAMP,
    DEPTH,
    NSAMP,
    N_SPACER,
    REGION_SAMPLES,
    SPW,
    address_differences,
    channel_delay,
    check_an_epoch_offset_moves_the_reading_too,
    check_both_waveforms_reached_the_air,
    check_every_window_is_whole_and_announced_clean,
    check_the_epochs_are_tied,
    check_the_reading_aliases,
    check_the_run_was_clean,
    check_the_two_ends_agree_on_phase,
    leading_silent_windows,
    measured_delay,
    responses,
    run_pysim,
    scenario_frames,
    window_frames,
    windows_as_codes,
)

#: **Recorded 2026-09-08 on the first green run.**  The raw address difference the capture carries,
#: and the two terms it is made of: the path's :data:`DELAY_SAMP` and the loop's own structural
#: block.  A change in the total is a finding either way — the point of the split is that only one
#: of the two terms is a property of the path.
WANT_RAW_DIFFERENCE = 160
WANT_LOOP_LATENCY_SAMP = 64

#: Windows the run produces, how many carry the first waveform, and how many arrive before any
#: sample does.  The middle number is the one the spacing buys: at :data:`N_SPACER` ``= 0`` it is
#: zero and every command still answers ``SHOT_LOADED``.
WANT_WINDOWS = 44
WANT_WINDOWS_OF_FIRST_WAVEFORM = 4
WANT_LEADING_SILENT_WINDOWS = 7

#: Samples that agree on the reading.  A **floor rather than a target** where it is used as one —
#: agreement over a handful of samples would not be evidence — but pinned here too, because a run
#: that quietly started carrying half as much waveform is a run measuring something else.
WANT_SAMPLES_IN_AGREEMENT = 4576

#: The one converter block the ADC's grid comes due for before anything has traversed the loop.
#: **Structural, and exactly the quantity :attr:`RfShotLoopbackTB.loop_blk_latency` declares** — a
#: converter cannot emit samples it has not yet collected.
WANT_ADC_UNDERRUN = 1


@pytest.fixture(scope="module")
def run() -> dict:
    """One loopback run at the demonstrated geometry, shared by the assertions below."""
    frames = scenario_frames()
    tb = run_pysim(frames=frames)
    return {"tb": tb, "frames": frames, "windows": window_frames(tb)}


# ---------------------------------------------------------------------------
# The headline
# ---------------------------------------------------------------------------

def test_the_channel_delay_is_an_address_difference(run):
    """**The whole reason this example exists.**

    Every captured sample is asked two questions — *which address were you sent from?* (its code,
    because the waveform is a ramp filling exactly one buffer) and *which address did you arrive at?*
    (its window's header plus its offset) — and the difference is the delay.

    The reading is the path's delay once the loop's **declared** structural latency is taken off.
    That subtraction is not a fit: :attr:`RfShotLoopbackTB.loop_blk_latency` sums one converter hop
    with what the nodes on the path declare, and
    :func:`test_the_loop_latency_is_declared_rather_than_fitted` shows the sum is right by driving
    the path at zero.
    """
    tb, wf = run["tb"], run["windows"]
    assert measured_delay(wf) == WANT_RAW_DIFFERENCE, (
        f"the raw address difference is {measured_delay(wf)}, recorded {WANT_RAW_DIFFERENCE}.")
    assert channel_delay(tb, wf) == DELAY_SAMP, (
        f"the path reads {channel_delay(tb, wf)} samples and was configured with {DELAY_SAMP}. "
        f"This is the claim the example is for; a workaround that made it pass would be worse than "
        f"a failure.")


def test_the_loop_latency_is_declared_rather_than_fitted(run):
    """The structural term is a property of the graph, and driving the path at **zero** shows it.

    With no path delay at all the address difference is exactly what the loop declares — one
    converter block — so the number subtracted from every other reading is not a residual chosen to
    make the arithmetic work.
    """
    tb = run["tb"]
    assert tb.loop_blk_latency == 1 and tb.loop_latency_samp == WANT_LOOP_LATENCY_SAMP
    assert tb.chan.blk_latency == 0, (
        "the path declares a block latency of its own; the loop's sum must follow it rather than "
        "the constant this gate remembers.")
    bare = run_pysim(delay_samp=0)
    wf = window_frames(bare)
    assert measured_delay(wf) == WANT_LOOP_LATENCY_SAMP, (
        f"with no path delay the address difference is {measured_delay(wf)}, and the graph declares "
        f"{WANT_LOOP_LATENCY_SAMP}. The structural term is fitted, not declared.")
    assert channel_delay(bare, wf) == 0


def test_the_two_ends_agree_on_phase(run):
    """**The pair's claim, and the only place it can be made.**

    S1 gates the transmitter's phase against its own memory and S2 the receiver's against its own.
    Neither can say the two are the *same* phase. Here every one of thousands of captured samples
    returns the same address difference — and a transmitter that slipped a pass, a receiver that
    mis-addressed one window, or a block lost anywhere on the loop each puts a second value in that
    histogram while still producing a capture that looks like a waveform.
    """
    d, n = check_the_two_ends_agree_on_phase(run["windows"], where="loopback: ")
    assert (d, n) == (WANT_RAW_DIFFERENCE, WANT_SAMPLES_IN_AGREEMENT), (
        f"{n} sample(s) agreed on {d}; recorded {WANT_SAMPLES_IN_AGREEMENT} on "
        f"{WANT_RAW_DIFFERENCE}.")


# ---------------------------------------------------------------------------
# The rule's other half
# ---------------------------------------------------------------------------

def test_the_reading_aliases_at_one_buffer():
    """A delay of ``D`` and one of ``D + NSAMP`` read the **same** number.

    An example that only demonstrated the working case would teach half the rule, and this is the
    half a reader trips over first: the reading is a difference of *addresses*, so it lives modulo
    the buffer and the geometry has to be chosen against the delay being measured.

    The two runs are also asserted to be *distinguishable by something else* — the longer path holds
    more silence before its first sample — because otherwise this gate would be comparing a run with
    itself. That difference is precisely the information an address discards and a timestamp keeps.
    """
    near, far = check_the_reading_aliases(where="alias: ")
    assert near == far == DELAY_SAMP % NSAMP
    assert ALIAS_DELAY_SAMP % NSAMP == DELAY_SAMP % NSAMP, (
        "the aliasing pair is not one buffer apart, so this gate is not about aliasing")


def test_an_epoch_offset_moves_the_reading_exactly_as_a_path_delay_does():
    """**What MTS buys you**, shown by taking it away.

    The path is untouched and the *transmitting* tile's counter is started one block late. The
    address difference moves by exactly one block — the same move a longer path would have made, and
    **the capture cannot tell you which it was**. ``t0`` is what pins the epoch to zero so that what
    is left in the address is the path.

    ``t0_tx`` rather than ``t0_rx``, and the asymmetry is a property of the model: starting the
    *receive* tile later can only cancel structural latency the loop already has, and past that a
    block simply waits in a queue. A gate that swept ``t0_rx`` would be asserting a saturation curve.
    """
    tied, late = check_an_epoch_offset_moves_the_reading_too(blocks=1, where="epoch: ")
    assert tied == WANT_RAW_DIFFERENCE
    assert late == (tied + BLKSIZE) % NSAMP


def test_the_epochs_are_tied_by_the_converter_and_not_by_the_testbench(run):
    """One converter, one epoch — asserted on the **interfaces**, not on the testbench's fields.

    ``Rfdc`` pushes ``t0`` onto every edge it binds, and it is that push that decides when each grid
    ticks. This is the first design in the repo to exercise the reason the converter carries both
    directions in one module: two converters would be two epochs, and an address difference between
    them would mean nothing.
    """
    tb = run["tb"]
    check_the_epochs_are_tied(tb, where="loopback: ")
    assert tb.epoch_offset_samp == 0, "the demonstration runs with MTS assumed, i.e. t0_rx == t0_tx"
    assert tb.dac_if.t0 == tb.adc_if.t0 == 0.0
    # The converter is on the TX side of the DAC edge and the RX side of the ADC edge, so ONE node
    # set both epochs -- which is what makes their equality structural rather than two fields that
    # happen to match.  `RFSampIF.set_t0` refuses a second, different owner outright.
    assert tb.dac_if.endpoints["tx"].comp is tb.rfdc
    assert tb.adc_if.endpoints["rx"].comp is tb.rfdc


# ---------------------------------------------------------------------------
# The controls
# ---------------------------------------------------------------------------

def test_a_relative_pair_reads_no_single_delay_at_all():
    """**THE NEGATIVE CONTROL.**  The same graph at ``absolute_index = 0``.

    Without it the address correspondence could be a property of the loopback — of one converter, one
    ramp and a quiet path — rather than of the mode, and every gate above would pass on a design that
    ignored the parameter entirely.

    It is one knob on one graph rather than a second graph, because what has to be isolated is the
    *parameter*: two graphs would differ in more than the thing under test.

    **What fails is unanimity, not correctness.** The relative pair captures perfectly good samples
    and announces perfectly good windows; what it cannot do is put them at addresses their own
    indices name, so the histogram splits.
    """
    tb = run_pysim(absolute_index=0)
    wf = window_frames(tb)
    hist = address_differences(wf)
    assert len(hist) > 1, (
        f"the relative pair read a single address difference {hist} anyway. Then absolute_index "
        f"changes nothing about this measurement and every gate above is asserting a property the "
        f"design already had.")
    with pytest.raises(AssertionError, match="do not agree on one address difference"):
        measured_delay(wf, where="relative: ")
    # ... and it is a control rather than a broken run: the windows are whole and clean.
    check_every_window_is_whole_and_announced_clean(wf, where="relative: ")


def test_the_first_waveform_is_lost_when_the_loads_are_too_close():
    """**The spacing control.**  Remove the spacers and the first waveform never reaches the air.

    Under ``absolute_index`` a playout is deferred to the next buffer boundary and **a load arriving
    inside that window cancels the arm outright**. The command path says nothing: every frame is
    still answered ``SHOT_LOADED``. Only the capture can tell you, which is why the scenario carries
    :data:`N_SPACER` refused frames and why this gate exists beside the one that says both waveforms
    played.

    Four spacers is the measured threshold at this geometry; two is not enough and the shipped
    scenario uses eight.
    """
    from waveflow.hw.rf_shot_tx import SHOT_LOADED

    assert N_SPACER > 4, (
        f"the shipped scenario uses {N_SPACER} spacer(s) and the measured threshold at this "
        f"geometry is 4. A margin of none is a scenario one edit away from measuring nothing, "
        f"silently.")
    for n_spacer in (0, 2):
        frames = scenario_frames(n_spacer=n_spacer)
        tb = run_pysim(frames=frames)
        wf = window_frames(tb)
        with pytest.raises(AssertionError, match="never reached the air"):
            check_both_waveforms_reached_the_air(wf, where=f"n_spacer={n_spacer}: ")
        loads = [(t, s) for t, s in responses(tb) if s == SHOT_LOADED]
        assert len(loads) == 2, (
            f"n_spacer={n_spacer}: the transmitter answered {loads}; BOTH loads are supposed to be "
            f"accepted — that is what makes this failure silent on the command path.")


def test_both_waveforms_reach_the_air_because_the_loads_are_spaced(run):
    """The positive half: with :data:`N_SPACER` spacers, both waveforms play and both are in phase.

    The reading is unanimous across *both* of them, which is the part a single waveform could not
    show: it says the correspondence is a property of the addressing rather than of one payload.
    """
    wf = run["windows"]
    n_a = check_both_waveforms_reached_the_air(wf, where="loopback: ")
    assert n_a == WANT_WINDOWS_OF_FIRST_WAVEFORM, (
        f"{n_a} window(s) carried the first waveform, recorded {WANT_WINDOWS_OF_FIRST_WAVEFORM}. "
        f"Falling to zero is the silent failure; falling to one is a margin worth knowing about.")
    assert len(address_differences(wf)) == 1


# ---------------------------------------------------------------------------
# The preconditions the measurement rests on
# ---------------------------------------------------------------------------

def test_the_run_loses_nothing_anywhere_on_the_loop(run):
    """Four counters, four different failures — and the receiver's is the one that matters most.

    Under absolute indexing a starved host costs the receiver a **whole window** rather than a block,
    so a demonstration fighting drops would be measuring its own sink. The single ADC underrun is
    structural and is pinned rather than tolerated: it is the converter's first grid tick, before
    anything has traversed the loop, and it is exactly the block
    :attr:`RfShotLoopbackTB.loop_blk_latency` declares.
    """
    tb = run["tb"]
    check_the_run_was_clean(tb, run["frames"], where="loopback: ")
    assert int(tb.adc_if.underrun) == WANT_ADC_UNDERRUN, (
        f"the ADC edge underran {int(tb.adc_if.underrun)} time(s), recorded {WANT_ADC_UNDERRUN} — "
        f"the one grid tick that comes due before the loop has delivered anything.")
    assert (tb.chan.n_in, tb.chan.n_out) == (int(tb.n_blk), int(tb.n_blk))


def test_every_window_is_whole_and_announced_clean(run):
    """The precondition of the measurement, not a re-gate of ``examples/rf_shot_rx``.

    That example proves the receiver's contract against a ramp it can see whole. What this asserts is
    narrower and load-bearing here: a short or ``CAP_LOST`` window would make ``window_abs_index``
    name an address the samples are not at, and the reading would be wrong for a reason that has
    nothing to do with the path.
    """
    wf = run["windows"]
    check_every_window_is_whole_and_announced_clean(wf, where="loopback: ")
    assert len(windows_as_codes(wf)) == WANT_WINDOWS
    assert leading_silent_windows(wf) == WANT_LEADING_SILENT_WINDOWS, (
        f"{leading_silent_windows(wf)} silent window(s) before the first sample, recorded "
        f"{WANT_LEADING_SILENT_WINDOWS}: the path's delay plus the transmitter's own deferral.")


# ---------------------------------------------------------------------------
# The graph
# ---------------------------------------------------------------------------

def test_the_graph_is_one_converter_two_absolute_buffers_and_a_path(run):
    """The structure the claim rests on, asserted where a reader will look for it.

    **One** converter with both directions, **both** buffers absolute, and the delay on a **node**
    rather than on either edge — which is what lets a path delay and an epoch offset be two different
    things a reader can tell apart.
    """
    tb = run["tb"]
    assert (tb.rfdc.n_rx, tb.rfdc.n_tx) == (1, 1)
    assert int(tb.tx.absolute_index) == 1 and int(tb.rx.absolute_index) == 1
    assert int(tb.tx.depth) == int(tb.rx.depth) == DEPTH, (
        "the two ends must share one depth, or 'sent from mem[j], arrives at mem[(j+D) mod depth]' "
        "is a statement about two different moduli.")
    assert int(tb.chan.delay_samp) == DELAY_SAMP
    # The delay is on the NODE.  An edge that carried it could only ever record it.
    assert not hasattr(tb.dac_if, "delay_samp") and not hasattr(tb.adc_if, "delay_samp")
    assert tb.dac_if.endpoints["rx"] is tb.chan.rf_in
    assert tb.adc_if.endpoints["tx"] is tb.chan.rf_out


def test_the_geometry_is_chosen_against_the_delay_being_shown():
    """The numbers this demonstration turns on, asserted so a later edit cannot quietly break them.

    :data:`DELAY_SAMP` is deliberately awkward — not a multiple of the converter block and not a
    multiple of a window — so a reading that came out right could not have come from a block index
    or a window index. And it is comfortably inside one buffer, because the reading aliases there.
    """
    assert DELAY_SAMP % BLKSIZE and DELAY_SAMP % REGION_SAMPLES, (
        f"delay {DELAY_SAMP} is a whole number of blocks or windows, so a coarser mechanism could "
        f"produce the same reading and this example would not be showing what it claims.")
    assert DELAY_SAMP % SPW == 0, "an address is a word; a delay that split one is not an address"
    assert 0 < DELAY_SAMP < NSAMP, "the demonstrated delay must fit inside one buffer"
    assert np.array_equal(
        np.unique(np.arange(CODE_A, CODE_A + NSAMP)), np.arange(CODE_A, CODE_A + NSAMP)), (
        "the waveform must be one ramp of distinct codes, or a code does not name its address")


def test_the_two_standalone_examples_are_untouched():
    """A third example, **not** a replacement.

    A loopback cannot isolate a TX defect from an RX one — a wrong address here could be either end —
    so ``examples/rf_shot_tx`` and ``examples/rf_shot_rx`` remain the per-design contracts. This gate
    is a tripwire: importing both examples' gate vocabulary here fails loudly if either is retired,
    rather than leaving this file as the only thing standing behind the pair.
    """
    from examples.rf_shot_rx.rf_shot_rx import check_addresses_are_the_phase  # noqa: F401
    from examples.rf_shot_tx.rf_shot_tx import (  # noqa: F401
        check_address_is_the_phase,
        check_starts_on_a_boundary,
    )
    from waveflow.hw.rf_shot_rx import RfShotRx
    from waveflow.hw.rf_shot_tx import RfShotTx
    from waveflow.simulation.simulation import Simulation

    # Both designs still DEFAULT to today's behaviour; this example opts in explicitly, which is
    # what keeps every recorded number in those two gate sets meaning what it meant.
    assert int(RfShotTx(sim=Simulation(), name="probe_tx").absolute_index) == 0
    assert int(RfShotRx(sim=Simulation(), name="probe_rx").absolute_index) == 0
