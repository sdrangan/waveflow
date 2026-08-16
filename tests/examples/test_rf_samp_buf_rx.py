"""The RX capture buffer in pysim — the four command cases, each with a PREDICTED result.

``plans/adc_model.md`` staging item 3 (RX).  The values here are not transcriptions of a run: a
captured sample is ``SAMP_BASE + idx`` whatever the timing did, so the whole expected output follows
from the commands.  What timing decides is *which case* each command exercised, and that is pinned
down by the ORDER rather than by the clock — see :data:`~examples.rf_samp_buf_rx.rf_samp_buf_rx.GATE_COMMANDS`
for the argument, command by command.

Two gates here are about the design rather than the data:

* **``dropped == 0`` on the converter's AXIS interface.** This design exists to satisfy condition 3
  of the fidelity contract (the DUT never stalls its input); if the ingress ever stalls, the point is
  lost.  It satisfies it structurally — the ingress writes a BRAM port, which cannot back-pressure.
* **The horizon counter driven off zero**, by a deliberately too-old command, with the count
  predicted.  A counter that has never counted is not evidence.
"""
from __future__ import annotations

import numpy as np
import pytest

from examples.rf_samp_buf_rx.rf_samp_buf_rx import (
    BUF_DEPTH,
    GATE_COMMANDS,
    HORIZON_MARGIN,
    RF_SAMP_BUF_OK,
    RF_SAMP_BUF_TOO_OLD,
    WORD_BW,
    RfSampBufRxTB,
    RxCmd,
    RxResp,
    captured_words,
    command_bursts,
    expected_capture,
    ramp_samples,
    responses,
    run_pysim,
    sdiff,
)


@pytest.fixture(scope="module")
def tb() -> RfSampBufRxTB:
    """One run, shared: the scenario is fixed and the run is the expensive part."""
    return run_pysim()


# ---------------------------------------------------------------------------
# The four cases
# ---------------------------------------------------------------------------

def test_the_captured_samples_are_the_predicted_ones(tb):
    """Every word, bit-exact against a prediction derived from the commands alone."""
    got = captured_words(tb)
    want, _ = expected_capture()
    assert got.size == want.size, f"captured {got.size} samples, predicted {want.size}"
    if not np.array_equal(got, want):
        bad = int(np.argmax(got != want))
        raise AssertionError(
            f"sample {bad}: got {int(got[bad])}, predicted {int(want[bad])} — a capture buffer's "
            f"whole failure mode is returning the WRONG WINDOW, which is why the samples are a ramp")


def test_every_command_gets_a_response_and_each_is_the_predicted_one(tb):
    """``(tid, status, nsent)`` per command — the counted contract.

    The response is what lets a host tell "your window fell off the buffer" from "the samples have
    not arrived yet". Without it, both look like a short read.
    """
    _, want = expected_capture()
    assert responses(tb) == want


def test_the_four_cases_were_actually_exercised(tb):
    """The cases are distinguished by *whether the capture had to wait*, and that is predicted too.

    Commands 1 (future) and 3 (straddling) must wait for the converter; command 2 (in the buffer) is
    served without waiting and command 4 is refused before it can wait. So ``n_waited == 2`` — and if
    it were 3, command 2 would have been served from the future rather than from the buffer, i.e. a
    different case than the one this scenario claims to test.
    """
    assert tb.dut.n_waited == 2, (
        f"{tb.dut.n_waited} commands waited, predicted 2 (the future and straddling cases). The "
        f"in-buffer command must NOT wait, or the scenario is not testing what it says.")


def test_the_straddling_window_is_contiguous_across_the_boundary(tb):
    """The case a trigger actually wants: pre-trigger from the buffer, then live samples.

    Command 3 asks for 100 samples starting at 3800 while the write pointer is at 3840 — 40 already
    in the buffer, 60 not yet arrived — and the output must be one unbroken ramp across the seam. A
    design that served the two halves from different places would show a discontinuity exactly there.
    """
    _tid, start, nsamp = GATE_COMMANDS[2]
    got = captured_words(tb)
    seg = got[-nsamp:]
    ramp = ramp_samples()
    assert np.array_equal(seg, ramp[start:start + nsamp])
    assert np.all(np.diff(seg.astype(np.int64)) == 1), "the seam is where a straddling capture breaks"


# ---------------------------------------------------------------------------
# The horizon, as a counted contract
# ---------------------------------------------------------------------------

def test_the_horizon_counter_is_driven_off_zero_by_a_too_old_request(tb):
    """Predicted: exactly one command (tid 4) falls off the horizon.

    A counter that never counted proves nothing, so the scenario contains a request that MUST be
    refused: sample 0 is 3840+ samples behind the write pointer by the time it is examined, and the
    buffer holds 1024.
    """
    assert tb.dut.n_too_old == 1
    assert (4, RF_SAMP_BUF_TOO_OLD, 0) in responses(tb)


def test_a_refused_command_emits_no_samples_at_all(tb):
    """Never a silent read. The alternative — returning whatever is at that address now — is data
    from the wrong time, which is worse than an error because nothing downstream can detect it."""
    _, want = expected_capture()
    assert sum(n for _t, _s, n in responses(tb)) == sum(n for _t, _s, n in want)
    assert captured_words(tb).size == sum(n for _t, s, n in responses(tb) if s == RF_SAMP_BUF_OK)


def test_the_usable_horizon_is_the_depth_minus_the_margin():
    """The margin is the price of the progress channel's staleness, and it is stated, not hoped for.

    ``last_wr`` only ever lags, which makes the "already written?" test harder to pass (safe: the
    capture waits longer) and the "not overwritten?" test *easier* (unsafe). The margin bounds the
    unsafe direction; without it the check would be exactly as stale as the channel.
    """
    assert HORIZON_MARGIN > 0
    assert BUF_DEPTH - HORIZON_MARGIN < BUF_DEPTH


# ---------------------------------------------------------------------------
# Condition 3 — the reason this design is shaped the way it is
# ---------------------------------------------------------------------------

def test_the_converter_never_had_a_sample_refused(tb):
    """``dropped == 0`` on the ADC's AXIS interface, with the converter actually attached.

    This is condition 3 of the fidelity contract, measured rather than asserted. The ingress
    satisfies it *structurally*: it writes a BRAM port, which has no handshake and cannot refuse it,
    so there is no FIFO depth to size and no argument to get wrong.
    """
    assert tb.adc_axis.dropped == 0, (
        f"the ADC offered {tb.adc_axis.dropped} words the fabric would not take — the ingress "
        f"stalled, which this design's structure is supposed to make impossible")


def test_every_sample_the_converter_sent_reached_the_buffer(tb):
    """The complement of the drop count: the ingress consumed the whole stream."""
    assert tb.dut.ingress.wr == ramp_samples().size % (1 << WORD_BW)


# ---------------------------------------------------------------------------
# The pieces the cases rest on
# ---------------------------------------------------------------------------

def test_the_circular_comparison_survives_the_counters_wrap():
    """A plain ``<`` on a wrapping counter is wrong the first time it wraps, and wrong silently."""
    assert sdiff(10, 5) == 5
    assert sdiff(5, 10) == -5
    # 3 samples after the wrap vs 5 before it: the signed difference is +8, not -65528.
    assert sdiff(3, 65531) == 8
    assert sdiff(65531, 3) == -8


def test_a_command_round_trips_through_its_schema():
    """The command is serialized by the schema, never by hand — the C++ side reads the same layout
    with a generated ``read_stream<16>``."""
    bursts = command_bursts()
    assert len(bursts) == len(GATE_COMMANDS)
    for burst, (tid, start, nsamp) in zip(bursts, GATE_COMMANDS):
        c = RxCmd().deserialize(burst, word_bw=WORD_BW)
        assert (int(c.tid), int(c.start), int(c.nsamp)) == (tid, start, nsamp)


def test_a_response_round_trips_through_its_schema():
    r = RxResp(tid=7, status=RF_SAMP_BUF_OK, nsent=64)
    back = RxResp().deserialize(np.asarray(r.serialize(word_bw=WORD_BW)), word_bw=WORD_BW)
    assert (int(back.tid), int(back.status), int(back.nsent)) == (7, RF_SAMP_BUF_OK, 64)


def test_the_buffer_holds_the_last_depth_samples(tb):
    """The memory is a circular buffer, so at the end it holds the newest ``depth`` samples."""
    ramp = ramp_samples()
    storage = np.asarray(tb.dut.mem.storage)
    for idx in (ramp.size - 1, ramp.size - BUF_DEPTH):
        assert storage[idx % BUF_DEPTH] == ramp[idx]
