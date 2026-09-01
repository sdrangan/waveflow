"""The shot transmitter in SimPy — ``plans/rf_shot_buf.md`` Stage B, without a toolchain.

The RTL gate (``tests/examples/test_rf_shot_play_xsi.py``) is the expensive half and needs Vivado.
This is the half that must hold on any checkout, and it carries the claims a byte comparison cannot
make: the five verdicts, the phase separation the buffer's whole safety argument rests on, and the
lowering the RTL gate would otherwise be the first thing to notice.

Every acceptance check here is the **same function** the RTL gate calls
(:func:`~examples.rf_shot_play.rf_shot_play.check_responses`,
:func:`~examples.rf_shot_play.rf_shot_play.check_played`), so the two backends are compared against
one golden rather than each against its own expectations.
"""
from __future__ import annotations

import numpy as np
import pytest

from examples.rf_shot_play.rf_shot_play import (
    BLKSIZE,
    GATE_FRAMES,
    NREPEAT,
    NWORD,
    SHORT_FRAMES,
    SHORT_WORDS,
    STARTUP_BLOCKS,
    WORD,
    check_played,
    check_responses,
    expected_plays,
    expected_responses,
    first_play_offset,
    played_samples,
    responses,
    run_pysim,
    shot_dense,
    shot_slots,
)
from waveflow.hw.rf_shot_tx import (
    SHOT_BUSY,
    SHOT_LOADED,
    SHOT_SHORT,
    SHOT_STATUS_NAMES,
    SHOT_WRONG_LEN,
    SHOT_ZERO_LEN,
)


@pytest.fixture(scope="module")
def gate():
    """The four-verdict run, once."""
    return run_pysim(frames=GATE_FRAMES)


@pytest.fixture(scope="module")
def short():
    """The truncated-transfer run, once — its own stream, for the reason in the example's docstring."""
    return run_pysim(frames=SHORT_FRAMES, in_bundle="vectors/cmd_short")


# ---------------------------------------------------------------------------
# The round trip
# ---------------------------------------------------------------------------

def test_the_shot_plays_nrepeat_times_bit_exactly(gate):
    """Load once, play three times, sample for sample.

    One comparison covers the command layer, the memory, the re-layout and the converter's own
    unpack — and it is made in converter **codes**, which is what a host wrote, rather than in the
    slot values the bus happens to carry them in.
    """
    check_played(played_samples(gate), NREPEAT, where="pysim: ")


def test_the_playout_starts_after_the_declared_transient(gate):
    """A converter fed through a pipeline **must** underrun until the first shot has been loaded.

    ``assert_clean`` checks that exactly rather than tolerating it: an underrun past the transient
    fails, and so does a shortfall — a design that declares two blocks of latency and exhibits one is
    a failure too.
    """
    gate.dac_if.assert_clean(STARTUP_BLOCKS)
    assert first_play_offset(played_samples(gate)) == STARTUP_BLOCKS * BLKSIZE


def test_the_player_played_whole_shots_and_the_phases_never_overlapped(gate):
    """Three claims a byte comparison does not make.

    A playout that stopped mid-shot has the right words in the right order for as far as it got, so
    the word count is checked against ``n_plays * nword`` rather than assumed from it; and the phase
    guard is asserted to have *run*, because a guard that never fired is evidence that something ran,
    not that the invariant held.
    """
    gate.dut.assert_played(NREPEAT)
    assert gate.dut.n_plays == NREPEAT
    assert gate.dut.play.n_words == NREPEAT * NWORD


# ---------------------------------------------------------------------------
# The verdicts
# ---------------------------------------------------------------------------

def test_every_header_is_answered_once_in_order(gate):
    """One response per header — the contract, and the evidence the in-band frame stayed aligned.

    Read off the response **stream** rather than off the module's own list, because the wire is what
    a host sees: a design that decided correctly and serialized wrongly passes every internal check.
    """
    check_responses(responses(gate), GATE_FRAMES, where="pysim: ")


def test_the_four_header_verdicts_are_all_reached(gate):
    """The scenario is only worth running if it reaches what it claims to.

    Named rather than implied: a gate whose stimulus quietly stopped exercising a branch is the
    failure this repo keeps meeting, and ``check_responses`` alone would pass a scenario that had
    become four copies of one case.
    """
    got = {s for _tid, s, _n in responses(gate)}
    assert got == {SHOT_LOADED, SHOT_BUSY, SHOT_WRONG_LEN, SHOT_ZERO_LEN}, (
        f"the gate scenario reached {sorted(SHOT_STATUS_NAMES[s] for s in got)}")


def test_a_load_arriving_mid_play_is_refused_rather_than_queued(gate):
    """:data:`~waveflow.hw.rf_shot_tx.SHOT_BUSY` is what stops the memory being overwritten under the
    reader — the one overlap :class:`~waveflow.hw.rf_shot_buf.ShotPhase` exists to make unreachable.

    So the refusal and the phase guard are two views of one invariant, and this asserts they agree:
    the second load was refused, and the guard never fired.
    """
    tid, status, loaded = responses(gate)[1]
    assert (tid, status, loaded) == (2, SHOT_BUSY, 0)
    assert gate.dut.phase.n_shots == 1, (
        f"{gate.dut.phase.n_shots} shots reached the memory; a refused load must not write it")


def test_malformed_is_reported_before_transient(gate):
    """``tid`` 3 is wrong **and** badly timed, and the design promises the fault the host can fix.

    A build that tested ``busy`` first would answer :data:`SHOT_BUSY` here — which is true, and
    useless: the host would retry forever against a length the buffer was never built for.
    """
    assert responses(gate)[2][1] == SHOT_WRONG_LEN


def test_a_zero_length_header_carries_no_payload_at_all(gate):
    """The empty-frame path: ``TLAST`` lands on the header beat itself.

    A distinct branch in both twins — the loop below it reads nothing and pads everything — and one
    that no other frame in either scenario exercises.
    """
    assert responses(gate)[3][1] == SHOT_ZERO_LEN
    assert np.asarray(GATE_FRAMES[3]).size == 1, "the zero-length frame must be the header alone"


# ---------------------------------------------------------------------------
# The short load — the reason the response exists
# ---------------------------------------------------------------------------

def test_a_short_transfer_says_so_and_says_how_much(short):
    """**The gate this design's response exists for.**

    A DMA transfer of exactly these bytes completes cleanly, so from the host side a half-loaded
    buffer is indistinguishable from a full one.  ``nsamp_loaded`` against the header's ``nsamp`` is
    the whole diagnosis, and it is a number the transfer cannot produce.
    """
    check_responses(responses(short), SHORT_FRAMES, where="pysim short: ")
    tid, status, loaded = responses(short)[0]
    assert status == SHOT_SHORT
    assert loaded == SHORT_WORDS * int(WORD.samp_per_word)
    assert loaded < int(short.dut.nsamp_shot), (
        "the point of this run is that what landed is LESS than what the header declared")


def test_a_short_shot_is_loaded_and_then_never_played(short):
    """Padded so the buffer's counted loop completes, and handed a repeat count of zero.

    The memory really does hold half a waveform — that is what a short transfer physically leaves
    behind — and the design's answer is not to un-write it but to refuse to play it.
    """
    assert short.dut.n_plays == 0
    assert expected_plays(SHORT_FRAMES) == 0
    check_played(played_samples(short), 0, where="pysim short: ")
    assert int(short.dac_if.underrun) == int(short.n_blk), (
        f"{int(short.n_blk) - int(short.dac_if.underrun)} block(s) reached the converter from a load "
        f"that was never playable")


def test_the_fence_behind_it_proves_the_loader_did_not_stall(short):
    """Without a ``TLAST`` pin the short frame would be a **hang**, not a verdict.

    The ``SHOT_END`` frame is answered, and headers are answered strictly in order — so its response
    is the evidence that the loader processed the short frame rather than waiting for words that were
    never coming.  A run that stalled would end with one response, not two.
    """
    got = responses(short)
    assert len(got) == 2 and got[1][1] == SHOT_LOADED


# ---------------------------------------------------------------------------
# The lowering
# ---------------------------------------------------------------------------

def test_the_composite_and_both_leaves_lower_cleanly():
    """``check(..., 'composite_kernel')`` on the design and on each task this stage added.

    Toolchain-free, and the first thing that would notice a graph the codegen cannot walk — which
    otherwise surfaces as a Vitis error a long way from the line that caused it.
    """
    from waveflow.build.codegen_check import check
    from waveflow.hw.rf_shot_tx import RfShotTx, ShotTxLoad, ShotTxPlay

    for cls in (ShotTxLoad, ShotTxPlay, RfShotTx):
        ok, why = check(cls, "composite_kernel")
        assert ok, f"{cls.__name__} does not lower to a composite kernel: {why}"


def test_the_gated_geometry_is_not_the_identity():
    """The caveat ``plans/rf_shot_buf.md`` § *The caveat* is about.

    Every configuration in this repo but the 4x2 preset has ``bits_per_samp == bits_per_samp_pack``,
    which makes the last stage a pair of wires — and a measurement of the identity is not a
    measurement of the re-layout.
    """
    tb = run_pysim(frames=SHORT_FRAMES, in_bundle="vectors/cmd_short")
    assert not tb.dut.is_identity
    assert int(WORD.justify_shift()) == 2


def test_the_host_writes_dense_words_and_the_converter_gets_slots():
    """What the logic-side port buys, stated as a comparison of two arrays.

    A host writes samples at the converter's *resolution* — four 14-bit values at 14-bit stride in a
    64-bit beat — and knows nothing about justification.  The two layouts really are different bytes,
    which is what makes the re-layout stage load-bearing rather than decorative.
    """
    dense, slots = shot_dense(), shot_slots()
    assert dense.size == slots.size == NWORD
    assert not np.array_equal(dense, slots), (
        "the dense and slot layouts are identical, so this build is measuring a pair of wires")


def test_the_expected_responses_are_derived_from_the_frames():
    """The golden is a function of the scenario, not a transcript of a run.

    A scenario edited without its golden is how a gate comes to assert what the design happens to do;
    deriving it means an edited frame moves both together or fails loudly.
    """
    assert expected_responses(GATE_FRAMES)[0] == (1, SHOT_LOADED, NWORD * int(WORD.samp_per_word))
    assert expected_responses(SHORT_FRAMES)[0] == (
        9, SHOT_SHORT, SHORT_WORDS * int(WORD.samp_per_word))
