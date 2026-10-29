"""Step 5 of plans/sionna_rt/sionna_rt.md: the channel applied block by block (AC3, AC4), numpy only."""

from __future__ import annotations

import numpy as np
import pytest

from tests.rfenv._pathsets import pathset, random_pathset
from waveflow.rfenv.apply import (
    DEFAULT_FILT_LEN,
    ChannelApplier,
    fractional_delay_kernel,
    reference_apply,
    split_delay,
    taps,
)

FS = 122.88e6


def rel_err(got: np.ndarray, want: np.ndarray) -> float:
    return float(np.linalg.norm(got - want) / np.linalg.norm(want))


def noise(shape, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return rng.standard_normal(shape) + 1j * rng.standard_normal(shape)


def run_blocks(
    app: ChannelApplier, x: np.ndarray, blksize: int, n0: int = 0
) -> np.ndarray:
    out = [
        app.process(x[:, i : i + blksize], n0 + i)
        for i in range(0, x.shape[1], blksize)
    ]
    return np.concatenate(out, axis=1)


# --- AC3: block continuity --------------------------------------------------------------------


@pytest.mark.parametrize("blksize", [1, 7, 64, 1024])
@pytest.mark.parametrize("delay_mode", ["absolute", "relative"])
def test_blocks_of_any_size_give_the_whole_signal_answer(blksize, delay_mode):
    """A 4x2 channel with Doppler, padding slots and delays up to 1 us (123 samples)."""
    ps = random_pathset(4, 2, 6, seed=3)
    x = noise((2, 2048), seed=4)
    want = reference_apply(ps, x, FS, delay_mode=delay_mode)
    got = run_blocks(ChannelApplier(ps, FS, delay_mode=delay_mode), x, blksize)
    assert got.shape == (4, 2048)
    assert rel_err(got, want) <= 1e-12


def test_the_answer_does_not_depend_on_where_the_run_starts():
    """The Doppler phase follows the absolute sample index: starting at n0 is the same signal."""
    ps = random_pathset(1, 1, 4, seed=5)
    x = noise((1, 512), seed=6)
    want = reference_apply(ps, x, FS, n0=10_000)
    got = run_blocks(ChannelApplier(ps, FS), x, 64, n0=10_000)
    assert rel_err(got, want) <= 1e-12


# --- AC3: fractional-delay accuracy -----------------------------------------------------------


def tones(n: np.ndarray, freqs: np.ndarray, phases: np.ndarray) -> np.ndarray:
    return np.exp(
        2j * np.pi * (freqs[:, None] * n[None, :] / FS + phases[:, None])
    ).sum(0)


@pytest.mark.parametrize("delay_samp", [0.1, 37.5, 200.9])
def test_a_sub_sample_delay_is_accurate_to_60_db(delay_samp):
    """40 tones within |f| <= 0.4 f_s through one path; the error excludes the start-up transient."""
    rng = np.random.default_rng(7)
    # Both band edges, and 38 tones between them.
    freqs = np.concatenate([[-0.4, 0.4], rng.uniform(-0.4, 0.4, 38)]) * FS
    phases = rng.uniform(0, 1, 40)
    n = np.arange(8192, dtype=np.float64)
    ps = pathset([[[1.0]]], [[[delay_samp / FS]]])
    app = ChannelApplier(ps, FS)
    y = run_blocks(app, tones(n, freqs, phases)[None, :], 512)[0]
    d = app.group_delay_samp
    want = tones(n - d - delay_samp, freqs, phases)
    keep = slice(app.history_len + DEFAULT_FILT_LEN, None)
    err_db = 10 * np.log10(
        np.mean(np.abs(y[keep] - want[keep]) ** 2) / np.mean(np.abs(want) ** 2)
    )
    assert err_db <= -60.0, err_db


@pytest.mark.parametrize("k", [17, 69, 1234])
def test_a_whole_sample_delay_is_exact(k):
    """k / FS * FS lands a few ulps off k for some k (69 and 1234 do); the delay is snapped, so the
    samples pass through bit for bit."""
    ps = pathset([[[0.5 - 0.25j]]], [[[k / FS]]])
    x = noise((1, k + 300), seed=8)
    app = ChannelApplier(ps, FS)
    y = app.process(x, 0)[0]
    lag = k + app.group_delay_samp
    np.testing.assert_array_equal(y[lag:], (0.5 - 0.25j) * x[0, :-lag])
    assert np.all(y[:lag] == 0)


def test_whole_sample_delays_snap_across_a_range():
    for k in range(1, 2000):
        assert split_delay(k / FS, FS, 32) == (k + 32, 0.0), k


def test_the_history_is_exactly_what_the_latest_path_reads():
    """A 200.9-sample path: K = floor(200.9 + 32) = 232, and the interpolator reads K + 32 back."""
    app = ChannelApplier(pathset([[[1.0]]], [[[200.9 / FS]]]), FS)
    assert app.history_len == 232 + 32


def test_the_kernel_at_zero_delay_is_an_impulse():
    c = fractional_delay_kernel(0.0, 64, 6.0)
    assert c.shape == (64,) and c[31] == 1.0 and np.all(np.delete(c, 31) == 0)


# --- AC4: Doppler -----------------------------------------------------------------------------


def test_the_doppler_phase_is_taken_at_n_minus_d():
    """Design note A1: y(n) = y_ref(n - D), so the rotation is exp(j2pi f_D (n - D) / f_s).  At 1 MHz
    the two readings differ by 1.64 rad, so a code that used n would fail here."""
    f_d, k, a = 1.0e6, 5, 0.3 + 0.4j
    ps = pathset([[[a]]], [[[k / FS]]], doppler=[[[f_d]]])
    x = noise((1, 512), seed=16)
    app = ChannelApplier(ps, FS)
    y = run_blocks(app, x, 64)[0]
    d = app.group_delay_samp
    n = np.arange(512)
    want = np.zeros(512, dtype=complex)
    want[k + d :] = (
        a * np.exp(2j * np.pi * f_d * (n[k + d :] - d) / FS) * x[0, : 512 - k - d]
    )
    np.testing.assert_allclose(y, want, rtol=0, atol=1e-13)


def test_doppler_shifts_a_tone_by_f_d_across_blocks():
    """350.2423 Hz is a receiver approaching at 30 m/s at 3.5 GHz (the spike's case)."""
    f_d, f0 = 350.2423, 1.0e6
    n = np.arange(2**18, dtype=np.float64)
    ps = pathset([[[1.0]]], [[[3.3e-7]]], doppler=[[[f_d]]])
    app = ChannelApplier(ps, FS)
    y = run_blocks(app, np.exp(2j * np.pi * f0 * n / FS)[None, :], 1024)[0]
    keep = slice(app.history_len, None)
    slope = np.polyfit(n[keep], np.unwrap(np.angle(y[keep])), 1)[0]
    measured = slope * FS / (2 * np.pi) - f0
    assert abs(measured - f_d) <= 0.1, measured


# --- dropped blocks and misuse ---------------------------------------------------------------


def test_a_dropped_block_is_zero_filled_and_counted():
    ps = random_pathset(2, 1, 3, seed=9)
    x = noise((1, 640), seed=10)
    app = ChannelApplier(ps, FS)
    out = []
    for i in range(0, 640, 64):
        if i == 192:  # this block never arrives
            continue
        out.append((i, app.process(x[:, i : i + 64], i)))
    assert app.dropped_samples == 64
    x_gap = x.copy()
    x_gap[:, 192:256] = 0
    want = reference_apply(ps, x_gap, FS)
    for i, y in out:
        np.testing.assert_allclose(y, want[:, i : i + 64], rtol=0, atol=1e-15)


def test_input_missing_before_the_first_block_is_a_counted_gap():
    """A node starts its applier at sample 0, so a first block that arrives later (the receiver
    dropped the ones before it) is a gap like any other: zero-filled and counted."""
    ps = random_pathset(1, 1, 3, seed=17)
    x = noise((1, 256), seed=18)
    app = ChannelApplier(ps, FS, start_n0=0)
    y = app.process(x[:, 128:], 128)
    assert app.dropped_samples == 128
    x_gap = x.copy()
    x_gap[:, :128] = 0
    np.testing.assert_allclose(
        y, reference_apply(ps, x_gap, FS)[:, 128:], rtol=0, atol=1e-15
    )


def test_blocks_must_arrive_in_order():
    app = ChannelApplier(pathset([[[1.0]]], [[[1e-7]]]), FS)
    app.process(np.zeros((1, 64)), 64)
    with pytest.raises(ValueError, match="in order"):
        app.process(np.zeros((1, 64)), 0)


@pytest.mark.parametrize(
    ("kw", "match"),
    [
        ({"filt_len": 63}, "even"),
        ({"delay_mode": "normalized"}, "delay_mode"),
        ({"samp_rate": 0}, "positive"),
    ],
)
def test_bad_parameters_are_refused(kw, match):
    args = {"samp_rate": FS} | kw
    fs = args.pop("samp_rate")
    with pytest.raises(ValueError, match=match):
        ChannelApplier(pathset([[[1.0]]], [[[1e-7]]]), fs, **args)


def test_a_block_with_the_wrong_channel_count_is_refused():
    app = ChannelApplier(random_pathset(4, 2, 3, seed=11), FS)
    with pytest.raises(ValueError, match="n_tx_ch=2"):
        app.process(np.zeros((4, 8)), 0)


# --- delay modes and MIMO ---------------------------------------------------------------------


def test_relative_mode_removes_the_earliest_path_delay():
    """Without Doppler, relative mode is absolute mode advanced by the bulk delay (10 samples)."""
    ps = pathset(
        [[[1.0, 0.3j]], [[0.5, 0.0]]],
        [[[10 / FS, 23.4 / FS]], [[12.5 / FS, -1.0]]],
        valid=[[[1, 1]], [[1, 0]]],
    )
    x = noise((1, 512), seed=12)
    rel = ChannelApplier(ps, FS, delay_mode="relative")
    assert rel.bulk_delay_s == 10 / FS
    y_rel = rel.process(x, 0)
    y_abs = ChannelApplier(ps, FS).process(x, 0)
    np.testing.assert_allclose(y_rel[:, :-10], y_abs[:, 10:], rtol=0, atol=1e-13)


def test_each_receive_channel_sums_its_transmit_channels():
    ps = random_pathset(4, 2, 5, seed=13)
    x = noise((2, 256), seed=14)
    both = ChannelApplier(ps, FS).process(x, 0)
    only = [
        ChannelApplier(ps, FS).process(np.where(np.arange(2)[:, None] == t, x, 0), 0)
        for t in range(2)
    ]
    np.testing.assert_allclose(both, only[0] + only[1], rtol=0, atol=1e-15)


def test_an_empty_channel_outputs_zeros():
    ps = pathset([[[0.0]]], [[[-1.0]]], valid=[[[False]]])
    assert not np.any(ChannelApplier(ps, FS).process(noise((1, 32), 15), 0))


# --- taps (the reference for AC5 and AC7) ------------------------------------------------------


def test_taps_follow_sionnas_formula():
    """A path on a whole lag is one tap; Doppler rotates it from one time step to the next."""
    a, f_d = 0.2 + 0.1j, 1000.0
    ps = pathset([[[a, 0.05]]], [[[3 / FS, 7.5 / FS]]], doppler=[[[f_d, 0.0]]])
    h = taps(ps, FS, 0, 10, num_time_steps=3)
    assert h.shape == (1, 1, 3, 11)
    want = a * np.eye(11)[3] + 0.05 * np.sinc(np.arange(11) - 7.5)
    np.testing.assert_allclose(h[0, 0, 0], want, rtol=0, atol=1e-15)
    rot = np.exp(2j * np.pi * f_d / FS)
    np.testing.assert_allclose(
        h[0, 0, 1] - 0.05 * np.sinc(np.arange(11) - 7.5),
        a * rot * np.eye(11)[3],
        rtol=0,
        atol=1e-15,
    )


def test_taps_rotate_at_the_sampling_frequency_not_the_bandwidth():
    """W sets the sinc's lag spacing; f_s sets the time steps.  Swapping them must fail."""
    w, fs, tau, f_d = 100e6, 200e6, 12.3e-9, 2.0e5
    ps = pathset([[[1.0]]], [[[tau]]], doppler=[[[f_d]]])
    h = taps(ps, w, -2, 5, sampling_frequency=fs, num_time_steps=4)
    lags = np.arange(-2, 6)
    for n in range(4):
        want = np.exp(2j * np.pi * f_d * n / fs) * np.sinc(lags - w * tau)
        np.testing.assert_allclose(h[0, 0, n], want, rtol=0, atol=1e-15)


def test_taps_ignore_padding_whatever_it_holds():
    ps = pathset(
        [[[1.0, 0.0]]], [[[0.0, -1.0]]], doppler=[[[0.0, np.nan]]], valid=[[[1, 0]]]
    )
    h = taps(ps, FS, 0, 3, num_time_steps=2)
    assert np.all(np.isfinite(h)) and h[0, 0, 1, 0] == 1.0
