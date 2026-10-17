import numpy as np
import pytest

from examples.dse_fir.fir_quality import FirSpec, design_quantized, max_representable


def test_declared_peak_bounds_quantized_l1():
    for width in (8, 12, 16):
        q = design_quantized(FirSpec(), 32, width, 2, input_peak=0.5)
        delta = 2.0 ** (2 - width)
        assert (0.5 + delta) * np.abs(q.taps_real).sum() <= max_representable(width, 2)
        assert q.r2 > 0.98
    with pytest.raises(ValueError, match="input_peak"):
        design_quantized(FirSpec(), 16, 8, 1, input_peak=1.0)


def test_fixed_path_signed_values_and_block_carry():
    from examples.dse_fir import fir_metrics as metrics

    params = {"ntap": 2, "samp_w": 12, "samp_i": 2, "unroll_lane": True, "mem_dwidth": 32}
    x = np.array([0.5, -0.5, 0.25, -0.25, 0.0] * 4)
    # Exactly representable two-tap averaging: independent convolution oracle.
    taps = np.array([512, 512])
    expected = np.convolve(x, [0.5, 0.5])[:len(x)]
    whole = metrics.run_fixed(x, taps, params, block_size=len(x))
    split = metrics.run_fixed(x, taps, params, block_size=3)
    serial = metrics.run_fixed(x, taps, {**params, 'unroll_lane': False}, block_size=1)
    np.testing.assert_array_equal(whole, expected)
    np.testing.assert_array_equal(split, whole)
    np.testing.assert_array_equal(serial, whole)


def test_response_matches_two_tap_analytic_shape_and_gain_invariance():
    from examples.dse_fir.fir_metrics import response_metrics

    spec = FirSpec(0.125, 0.25)
    a = response_metrics(np.array([0.5, 0.5]), spec, 8192)
    b = response_metrics(np.array([1.5, 1.5]), spec, 8192)
    # |H(f)| = cos(pi*f), inclusive band edges on this grid.
    freqs = np.fft.rfftfreq(8192)
    gain = np.cos(np.pi * freqs[freqs <= spec.f_pass]).mean()
    assert a['stopband_atten_db'] == pytest.approx(20 * np.log10(gain / np.cos(np.pi / 4)))
    assert a['passband_ripple_db'] == pytest.approx(-20 * np.log10(np.cos(np.pi / 8)))
    assert a['stopband_atten_db'] == pytest.approx(b['stopband_atten_db'])
    assert a['passband_ripple_db'] == pytest.approx(b['passband_ripple_db'])


def test_evaluate_finite_deterministic_quality_and_common_drive():
    import json

    from examples.dse_fir.fir_metrics import EVALUATOR_VERSION, evaluate

    params = {"ntap": 32, "samp_w": 16, "samp_i": 2, "unroll_lane": True, "mem_dwidth": 32}
    result = evaluate(params, {})
    assert EVALUATOR_VERSION == 'fir-quality-v1'
    assert result == evaluate(params, {})
    json.dumps(result, allow_nan=False)
    assert result['passband_sndr_db'] > 35
    assert result['stopband_rej_db'] > 35
    assert result['throughput_samp_per_cyc'] == 2
    # Input AP_TRN adds DC too, subsequently amplified by the tap DC gain.
    dc_gain = sum(result['metadata']['taps']['real'])
    assert abs(result['dc_bias'] + (1 + dc_gain) * 2.0 ** -15) < 2.0 ** -17
    assert result['metadata']['headroom']['input_peak'] == 0.5
    coarse = evaluate({**params, 'samp_w': 8, 'unroll_lane': False}, {})
    assert coarse['metadata']['headroom']['input_peak'] == 0.5
    assert coarse['throughput_samp_per_cyc'] == 1
    assert result['stopband_rej_db'] > coarse['stopband_rej_db']


def test_evaluate_rejects_invalid_measurements():
    from examples.dse_fir.fir_metrics import evaluate

    params = {"ntap": 16, "samp_w": 12, "samp_i": 2, "unroll_lane": True, "mem_dwidth": 32}
    for evaluation in ({'nsamp': 3}, {'nfft': 16}, {'input_peak': float('nan')},
                       {'f_pass': 0.4}, {'seed': -1}, {'surprise': 1}):
        with pytest.raises(ValueError):
            evaluate(params, evaluation)
    for bad in ({'samp_w': 33}, {'unroll_lane': 'false'}, {'ntap': 2.5}):
        with pytest.raises(ValueError):
            evaluate({**params, **bad}, {})
    with pytest.raises(ValueError, match='degenerate'):
        evaluate({**params, 'samp_i': 12}, {})


def test_fractional_alignment_matches_analytic_sinusoid():
    from examples.dse_fir.fir_metrics import _db_ratio, bandlimited, frac_delay

    n = np.arange(256)
    x = np.cos(2 * np.pi * 17 * n / 256)
    np.testing.assert_allclose(frac_delay(x, 7.5), np.cos(2 * np.pi * 17 * (n - 7.5) / 256), atol=2e-14)
    wave = bandlimited(256, 0.1, 0.2, 0.5, 3)
    assert np.abs(wave).max() == pytest.approx(0.5)
    freq = np.fft.rfftfreq(256)
    assert np.abs(np.fft.rfft(wave)[(freq < 0.1) | (freq > 0.2)]).max() < 1e-14
    assert _db_ratio(0, 0) == -300
    assert _db_ratio(1, 0) == 300


@pytest.mark.parametrize('width', [8, 12, 16, 24])
def test_headroom_on_adversarial_input_and_quantization_oracle(width):
    from examples.dse_fir.fir_metrics import run_fixed

    q = design_quantized(FirSpec(), 32, width, 2, input_peak=0.5)
    params = {"ntap": 32, "samp_w": width, "samp_i": 2, "unroll_lane": True, "mem_dwidth": 32}
    x = np.tile(0.5 * np.sign(q.taps_real[::-1]), 3)
    x[0] = -0.123456  # nonrepresentable input exercises AP_TRN too
    delta = 2.0 ** (2 - width)
    expected = np.floor(np.convolve(np.floor(x / delta) * delta, q.taps_real)[:len(x)] / delta) * delta
    actual = run_fixed(x, q.stored, params, block_size=7)
    np.testing.assert_array_equal(actual, expected)
    assert np.abs(actual).max() <= max_representable(width, 2)
