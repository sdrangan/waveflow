"""The TX sample buffer in pysim — the playout cases, each with a PREDICTED result.

``plans/adc_model.md`` staging item 3, TX half.  The mirror of ``test_rf_samp_buf_rx.py``, and the
asymmetry is what these tests are about: RX fails by **overrunning** (an ADC cannot be
back-pressured), TX fails by **underrunning** (a DAC consumes a word every sample period whether or
not one is ready).

Three claims that are about the design rather than the data:

* **The played samples are bit-exact in the primed window**, read off the far side of a real
  converter — so the check covers packing, the circular buffer, playout and the converter's own
  unpack, not just the buffer.
* **The underrun counter is driven off zero**, and driven *further* off zero by a loader that cannot
  keep up.  A counter that has never counted is not evidence.
* **A window whose slot has already played is refused**, not silently written.  Placing samples at a
  moment that has gone is the one failure a playout buffer must never commit.
"""
from __future__ import annotations

import numpy as np
import pytest

from examples.rf_samp_buf_tx.rf_samp_buf_tx import (
    GATE_COMMANDS,
    LATE_TIDS,
    PRIMED_AT,
    RF_SAMP_BUF_MISALIGNED,
    RF_SAMP_BUF_OK,
    RF_SAMP_BUF_TOO_LATE,
    SAMP_BW,
    RfSampBufTxTB,
    TxCmd,
    TxResp,
    TX_BUF_DEPTH,
    XSI_BLKSIZE,
    command_frame,
    expected_responses,
    find_loaded_run,
    played_samples,
    ramp_samples,
    responses,
    run_pysim,
)
from waveflow.hw.rf_samp_buf import BUF_DEPTH, unpack_samples
from waveflow.hw.rf_samp_buf_tx import RfSampBufLoader, RfSampBufPlayer, RfSampBufTx
from waveflow.hw.rfdc_samp_word import Rfsoc4x2SampWord as WORD
from waveflow.simulation.simulation import Simulation

#: Samples of the primed window the gate checks.  The two OK commands place 1024 samples from
#: :data:`PRIMED_AT`, and the DAC captures 8 blocks of 256, so the checkable overlap is what the sink
#: actually saw.
WINDOW = 1024


@pytest.fixture(scope="module")
def tb() -> RfSampBufTxTB:
    """One run, shared: the scenario is fixed and the run is the expensive part."""
    return run_pysim()


def _run_len(tb) -> int:
    """Contiguous, in-order, bit-exact loaded samples out of the converter.

    Measured by search rather than at a fixed offset, because **neither backend may assume played
    sample i is slot i**: the DAC emits blocks as it gets them and zero-fills when it does not, so
    the offset depends on the priming transient.  See :func:`find_loaded_run`.
    """
    return find_loaded_run(played_samples(tb))


# ---------------------------------------------------------------------------
# The playout path
# ---------------------------------------------------------------------------

def test_the_loaded_samples_come_out_of_the_converter_in_order(tb):
    """Bit-exact and unbroken, read off the RF sink — the far side of a real converter.

    A ramp is used precisely so a wrong SLOT is visible: a circular buffer's whole failure mode is
    playing the right *number* of samples from the wrong place.  At least a whole DAC block must
    emerge contiguously; the run is not asserted to the full 1024 because the converter zero-fills
    whole blocks during the priming transient, which splits the stream for a reason that belongs to
    the DAC rather than to this buffer.
    """
    n = _run_len(tb)
    assert n >= XSI_BLKSIZE, (
        f"only {n} loaded samples came out contiguously, expected at least one {XSI_BLKSIZE}-sample "
        f"block. A short run means the player emitted slots the loader had placed, out of order or "
        f"from the wrong address.")


def test_every_command_gets_the_predicted_response(tb):
    """``(tid, status, nloaded)`` per command — the counted contract."""
    assert responses(tb) == expected_responses(1)


def test_the_priming_transient_is_visible_and_counted(tb):
    """The player is FREE-RUNNING: it walks slots from t=0 whether or not anything is loaded.

    So the slots below the priming point play whatever the buffer held, and that is not a defect —
    it is what a DAC does before its buffer is filled, and the reason a real design primes before
    enabling the tile.  Asserted rather than tolerated, because it is the visible half of the
    never-miss-a-deadline contract: the counter must be off zero and must not be everything.
    """
    assert 0 < tb.dut.n_underrun < tb.dut.n_played, (
        f"underrun={tb.dut.n_underrun} of {tb.dut.n_played} played: the priming transient is "
        f"structural and must show, but it must not be the whole run")


# ---------------------------------------------------------------------------
# Too late — the mirror of RX's "too old"
# ---------------------------------------------------------------------------

def test_a_command_whose_slot_already_played_is_refused_and_counted(tb):
    """Predicted: exactly one command (tid 3) arrives after its slot has gone out of the DAC.

    Refused rather than written, because writing it would place samples at a moment that has passed
    — they would play a whole lap later, at the wrong time, and nothing downstream could detect it.
    That is the TX mirror of RX's ``TOO_OLD``, and it is a distinct status for a reason: "the data
    you wanted was overwritten" and "the data you sent arrived too late" are opposite failures.
    """
    assert tb.dut.n_too_late == len(LATE_TIDS) == 1
    assert (3, RF_SAMP_BUF_TOO_LATE, 0) in responses(tb)


def test_a_refused_command_places_no_samples_at_all(tb):
    """Never a partial write.  ``nloaded`` is the counted half of that promise."""
    for tid, status, nloaded in responses(tb):
        if status != RF_SAMP_BUF_OK:
            assert nloaded == 0, f"command {tid} was refused ({status}) but reports {nloaded} loaded"


def test_a_refused_command_does_not_desynchronise_the_frame(tb):
    """**The one place this body differs from the RX capture**, and it must be tested.

    The payload is IN-BAND, so a refused command whose words were left in the stream would make the
    next read take a sample for a ``tid``.  The loader therefore drains the whole frame whatever the
    verdict.  Evidence: the command *after* the refused one is answered, correctly and with its own
    tid — which is impossible if the frame slipped.
    """
    got = responses(tb)
    assert [t for t, _s, _n in got] == [t for t, _s, _n in GATE_COMMANDS], (
        "the responses are not one-per-command in order; the in-band frame desynchronised")
    after = [r for r in got if r[0] == 4][0]
    assert after[1] == RF_SAMP_BUF_OK and after[2] == 6, (
        f"the command after the refused one came back as {after}, so the frame slipped")


def test_the_loader_holds_a_command_the_buffer_has_no_room_for_yet():
    """The "too far ahead" case, provoked deliberately: a command naming a slot one whole buffer
    ahead of the play pointer cannot be placed until the player has walked far enough.

    The loader simply HOLDS — it is allowed to, because nothing downstream misses a deadline while it
    waits, which is the freedom that lets a host queue playout ahead of time.  Its own gate scenario
    primes only one block ahead and so never waits; this is a separate run rather than a coincidence
    of that one.
    """
    tb = run_pysim(tb=RfSampBufTxTB(name="far", sim=Simulation()),
                   cmds=((1, TX_BUF_DEPTH, 512),))
    assert tb.dut.n_waited == 1, (
        f"a command one whole buffer ahead did not have to wait (n_waited={tb.dut.n_waited}); the "
        f"'too far ahead' case is not being exercised")
    assert responses(tb) == [(1, RF_SAMP_BUF_OK, 512)], (
        "holding is not refusing: the command must be placed once the room appears")


# ---------------------------------------------------------------------------
# Word alignment — identical convention to RX, in both directions
# ---------------------------------------------------------------------------

def test_a_window_that_is_not_a_whole_number_of_words_is_refused():
    """Refused, not rounded — the same rule and the same status code as the RX side.

    Command 4 is legal at one and two samples per word and misaligned at four, which is the point:
    the verdict is a property of the geometry, and the same command must be refused wherever a
    partial word would otherwise be silently rounded.
    """
    for spw, want_status in ((1, RF_SAMP_BUF_OK), (2, RF_SAMP_BUF_OK),
                             (4, RF_SAMP_BUF_MISALIGNED)):
        tb = run_pysim(tb=RfSampBufTxTB(name=f"al{spw}", sim=Simulation(),
                                        word=WORD.specialize(samp_per_word=spw)))
        got = {t: s for t, s, _n in responses(tb)}
        assert got[4] == want_status, (
            f"at samp_per_word={spw} command 4 (nsamp=6) came back {got[4]}, expected {want_status}")
        assert tb.dut.n_misaligned == (1 if want_status == RF_SAMP_BUF_MISALIGNED else 0)


# ---------------------------------------------------------------------------
# samp_per_word > 1 — the throughput lever, and where slot order becomes observable
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def tb_by_width() -> dict:
    """One run per word geometry.  Same commands, same ramp, same played samples."""
    return {spw: run_pysim(tb=RfSampBufTxTB(name=f"w{spw}", sim=Simulation(),
                                            word=WORD.specialize(samp_per_word=spw)))
            for spw in (1, 2, 4)}


@pytest.mark.parametrize("spw", [1, 2, 4])
def test_the_same_samples_play_at_every_word_width(tb_by_width, spw):
    """Widening the word changes the *rate*, never what comes out of the converter.

    The comparison is in SAMPLES on the far side of the DAC, so it goes through the generated
    serializer in both directions — a slot-order mistake fails here rather than passing because both
    sides made it.  At one sample per word slot order is unobservable, which is exactly why this
    sweep has to include a width where it is not.
    """
    n = _run_len(tb_by_width[spw])
    assert n >= XSI_BLKSIZE, (
        f"spw={spw}: only {n} loaded samples came out contiguously. The ramp shows a wrong slot; "
        f"the unpack through the generated serializer shows a wrong lane.")


@pytest.mark.parametrize("spw", [1, 2, 4])
def test_a_wider_word_costs_fewer_firings_for_the_same_samples(tb_by_width, spw):
    """The throughput lever: one firing moves one WORD, so ``spw`` samples ride on each.

    **Two divisors, and they are different**, which is the thing to get right here.
    ``capacity_samp_per_cycle`` is the PLAYER's — 1 cycle per word since it became an II=1 loop — and
    ``max_samp_rate`` is the whole BUFFER's, which is its slowest stage, the loader at 2.  They were
    the same number while the player was the slowest, and a test written then would pass with either.

    Both divisors are read off their classes rather than written as literals, so a corrected constant
    cannot leave a stale number here.
    """
    tb = tb_by_width[spw]
    assert tb.rfdc.axis_bitwidth == SAMP_BW * spw
    assert tb.dut.capacity_samp_per_cycle == spw / RfSampBufPlayer.cycles_per_word
    assert tb.dut.max_samp_rate(300e6) == 300e6 * spw / tb.dut.cycles_per_word
    assert tb.dut.nsamp_held == TX_BUF_DEPTH * spw


def test_the_in_band_frame_round_trips_through_the_schema_and_the_serializer():
    """The command is serialized by the schema and the payload packed by the generated serializer —
    never by hand, in either case.

    Checked at four samples per word, because that is the width at which a hand-rolled pack would be
    wrong and a test at one would not notice.
    """
    spw, word_bw = 4, SAMP_BW * 4
    frame = command_frame(((7, 64, 8),), samp_per_word=spw)
    assert len(frame) == 2, "a frame is a command burst and a payload burst"
    cmd = TxCmd().deserialize(frame[0], word_bw=word_bw)
    assert (int(cmd.tid), int(cmd.start), int(cmd.nsamp)) == (7, 64, 8)
    assert frame[1].size == 8 // spw, "the payload is nsamp/spw words"
    assert np.array_equal(unpack_samples(frame[1], word_bw, spw), ramp_samples(4096)[64:72])


def test_a_response_round_trips_through_its_schema():
    r = TxResp(tid=9, status=RF_SAMP_BUF_TOO_LATE, nloaded=0)
    back = TxResp().deserialize(np.asarray(r.serialize(word_bw=SAMP_BW)), word_bw=SAMP_BW)
    assert (int(back.tid), int(back.status), int(back.nloaded)) == (9, RF_SAMP_BUF_TOO_LATE, 0)


# ---------------------------------------------------------------------------
# The rate contract
# ---------------------------------------------------------------------------

def test_a_dac_faster_than_the_player_is_refused():
    """The mirror of the RX rate check, and it fails the other way: too fast a converter costs RX
    dropped samples and costs TX underruns.

    **The ceiling is ``samp_per_word * f_axis / 2``, and the 2 is the LOADER's**, not the player's.
    The player reached II=1 on 2026-08-18; the loader did not, so the half costs what the loader
    costs.  It was ``f_axis / 3`` per sample-per-word while the player was the slowest stage, and
    ``f_axis / 2`` before that while the player's constant was inherited from the RX ingress rather
    than measured — the same number as today, arrived at for a different reason.
    """
    dut = RfSampBufTx(name="cap", sim=Simulation(), bitwidth=SAMP_BW, samp_per_word=1)
    assert dut.max_samp_rate(300e6) == 150e6
    with pytest.raises(ValueError, match="exceeds what the player can sustain"):
        dut.check_rate(400e6, 300e6)
    # The band the corrections were about: 120-140 MSa/s was refused while the player cost 3, and
    # 256 was refused while it cost 2.  All of it is legal now, and that is the result.
    for rate in (120e6, 140e6):
        assert dut.check_rate(rate, 300e6) < 1.0
    with pytest.raises(ValueError, match="exceeds what the player can sustain"):
        dut.check_rate(256e6, 300e6)
    assert dut.check_rate(120e6, 300e6) == pytest.approx(0.8)
    wide = RfSampBufTx(name="wide", sim=Simulation(), bitwidth=SAMP_BW * 4, samp_per_word=4)
    assert wide.check_rate(256e6, 300e6) < 1.0


def test_samp_per_word_must_be_a_power_of_two():
    """The sample->word conversion sits in the never-miss path, where a divide costs cycles the DAC
    does not give back — so it must be a shift."""
    with pytest.raises(ValueError, match="must be a power of two"):
        RfSampBufLoader(name="odd", sim=Simulation(), bitwidth=48, samp_per_word=3)
