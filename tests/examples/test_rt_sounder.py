"""examples/rt_sounder (plans/sionna_rt/sionna_rt.md, M5): the ray-traced channel sounder.

Runs without Sionna: the channel is the committed ``examples/rt_sounder/data/street_canyon`` file.
``tests/rfenv/test_no_sionna.py`` runs this module under the Sionna import blocker too.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from examples.rt_sounder.rt_sounder import (
    BLKSIZE,
    FULL_SCALE,
    SAMP_RATE,
    SOURCE_BLKSIZE,
    SOURCE_NBLK,
    SOURCE_SAMP_RATE,
    SPEC,
    TAP_WINDOW_DB,
    TARGET_DBFS,
    ZC_AMPLITUDE,
    adc_quantized,
    adc_samples,
    channel,
    compare_taps,
    estimate_taps,
    run_channel_mode,
    run_source_mode,
)
from waveflow.rfenv.apply import taps


def test_the_committed_channel_belongs_to_the_scene():
    # load_or_trace(..., retrace="never") refuses a file traced for another spec.
    ps = channel()
    assert ps.manifest["spec_hash"] == SPEC.hash()
    assert (ps.n_rx_ch, ps.n_tx_ch) == (1, 1) and int(ps.valid.sum()) == 5


def test_the_channel_has_a_cluster_and_a_distant_echo():
    """Taps above −30 dB: the cluster across lags 23-37 and the echo at lags 43-44."""
    ps = channel()
    h = taps(ps, SAMP_RATE, 0, math.ceil(ps.max_delay() * SAMP_RATE) + 4)[0, 0, 0]
    db = 20 * np.log10(np.abs(h) / np.abs(h).max())
    strong = np.flatnonzero(db > -30).tolist()
    assert strong == list(range(23, 38)) + [43, 44]
    assert int(np.argmax(np.abs(h))) in (30, 31)


# ---------------------------------------------------------------------------
# Channel mode (step 15, AC7): ZC -> Rfdc DAC -> RtChannel -> Rfdc ADC -> correlation
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def sounded():
    return run_channel_mode()


def test_channel_mode_the_loop_ran_clean(sounded):
    """Nothing lost: the ADC edge underran only for the declared converter hop, every grid block
    reached the sink, and no sample sat at the converter's rail."""
    tb = sounded
    tb.dac_if.assert_clean()
    tb.adc_if.assert_clean(startup_blocks=tb.loop_blk_latency)
    tb.chan.assert_ran()
    assert tb.chan.dropped_blocks == 0
    assert tb.dac_axis.dropped == 0 and tb.adc_axis.dropped == 0
    y = adc_samples(tb)
    assert y.size == tb.n_blk * BLKSIZE
    assert max(np.abs(y.real).max(), np.abs(y.imag).max()) < FULL_SCALE


def test_channel_mode_the_strongest_tap_sits_at_the_target_level(sounded):
    """AC7's setting: ``gain_db`` puts the strongest tap's share of the ZC at −10 dBFS."""
    tb = sounded
    h = estimate_taps(tb)
    # The strongest tap's share at the ADC, in sqrt(W): the DAC's amplitude through the tap and gain.
    amp = 10 ** (tb.gain_db / 20) * np.abs(h).max() * ZC_AMPLITUDE * FULL_SCALE
    level = 20 * math.log10(amp / FULL_SCALE)
    assert abs(level - TARGET_DBFS) <= 0.5


def test_channel_mode_recovers_the_ray_traced_taps(sounded):
    """AC7: every tap within 30 dB of the strongest at the same lag, |h|^2 within 0.5 dB."""
    cmp = compare_taps(sounded.paths, estimate_taps(sounded))
    print("\n" + cmp.table())
    assert [r[0] for r in cmp.rows] == list(range(23, 38)) + [43, 44]
    assert cmp.peak_lags[0] == cmp.peak_lags[1]
    assert cmp.worst_db <= 0.5, cmp.table()
    # The same lags both ways: nothing the estimate puts above the window (with the same 0.5 dB)
    # sits at a lag where Sionna has no such tap.
    assert cmp.spurious[1] <= -TAP_WINDOW_DB + 0.5, cmp.table()


def test_channel_mode_only_the_declared_latency_passes_ac7(sounded):
    """The comparison has teeth: read at any other latency within a block either way -- one sample
    early or late, or a whole converter hop -- the same estimate fails AC7."""
    h = estimate_taps(sounded)
    passing = [
        s
        for s in range(-BLKSIZE, BLKSIZE + 1)
        if compare_taps(sounded.paths, np.roll(h, s)).worst_db <= 0.5
    ]
    assert passing == [0]


# ---------------------------------------------------------------------------
# Source mode (step 16, AC6's pysim side at the XSI geometry): RtSignalSource -> ADC ->
# rf_pass_through -> DAC -> sink
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def source_run():
    return run_source_mode()


def test_source_mode_runs_clean(source_run):
    """The ADC edge, fed straight from the source, never underran; the DAC edge underran exactly
    for the declared loop (the ADC's hop and the DUT's block)."""
    tb, sent, got = source_run
    tb.adc_if.assert_clean()
    tb.dac_if.assert_clean(startup_blocks=tb.loop_blk_latency)
    assert tb.loop_blk_latency == 2
    assert len(sent) == len(got) == tb.n_blk == SOURCE_NBLK
    assert tb.dut.n_blk == tb.n_blk


def test_source_mode_the_bundle_reads_back_as_the_sources_blocks(source_run):
    """The rendered bundle is the source's own computation, bit for bit, noise included."""
    tb, sent, _ = source_run
    gen = tb.source.blocks(SOURCE_BLKSIZE, SOURCE_SAMP_RATE)
    for k, blk in enumerate(sent):
        assert np.array_equal(blk, next(gen)), f"block {k}"
    # Thermal noise is on: before the first path's interpolator span reaches the receiver, the
    # block holds noise and nothing else.
    first = math.floor(tb.paths.min_delay() * SOURCE_SAMP_RATE)
    assert first > 0 and np.all(sent[0][0, :first] != 0)


def test_source_mode_the_capture_is_the_quantized_source_shifted_by_the_declared_latency(
    source_run,
):
    """The sink's block k is the ADC-quantized source block k - L, exactly; the first L are the
    DAC's startup zero-fill."""
    tb, sent, got = source_run
    n_lat = tb.loop_blk_latency
    for k in range(n_lat):
        assert not np.any(got[k]), f"block {k} is inside the startup transient"
    for k in range(n_lat, len(got)):
        assert np.array_equal(got[k], adc_quantized(sent[k - n_lat])), f"block {k}"
