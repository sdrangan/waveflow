"""Deterministic FIR measurements through the existing bit-exact compute leaf.

These are Python golden measurements, not RTL verification or measured timing.
Worst-bin tap attenuation and waveform-average rejection have no ordering theorem.
"""
from __future__ import annotations

import numpy as np

from examples.fir_block.fir_block import FirCompute, pack_samples, unpack_samples
from waveflow.hw.fixpoint import from_real

EVALUATOR_VERSION = 'fir-quality-v1'
DB_FLOOR, DB_CEILING = -300.0, 300.0


def _db_ratio(numerator: float, denominator: float, factor: float = 10.0) -> float:
    """Finite reporting convention: absent signal is failure, absent noise is ceiling."""
    if numerator <= 0:
        return DB_FLOOR
    if denominator <= 0:
        return DB_CEILING
    return float(np.clip(factor * (np.log10(numerator) - np.log10(denominator)),
                         DB_FLOOR, DB_CEILING))


def response_metrics(taps, spec, nfft: int) -> dict:
    """Inclusive bands; mean passband magnitude normalization; worst-bin stop leak."""
    magnitude = np.abs(np.fft.rfft(taps, nfft))
    frequencies = np.fft.rfftfreq(nfft)
    pb = magnitude[frequencies <= spec.f_pass]
    sb = magnitude[frequencies >= spec.f_stop]
    gain = float(pb.mean())
    return {'stopband_atten_db': _db_ratio(gain, float(sb.max()), 20),
            'passband_ripple_db': (_db_ratio(float(pb.max()), float(pb.min()), 20)
                                   if gain > 0 else DB_CEILING),
            'passband_gain': gain}


def run_fixed(x, stored_taps, params: dict, *, block_size: int = 257) -> np.ndarray:
    """Quantize inputs, pack transport words and retain the leaf's carry across blocks."""
    if block_size < 1:
        raise ValueError('block_size must be positive')
    from waveflow.simulation.simulation import Simulation

    fir = FirCompute(name='quality', sim=Simulation(), **params)
    fir.load_taps(pack_samples(stored_taps, fir.samp_cls, params['mem_dwidth']),
                  len(stored_taps), fir.taps)
    stored = np.asarray(from_real(np.asarray(x, dtype=float), fir.samp_cls), dtype=np.int64)
    result = []
    for start in range(0, len(stored), block_size):
        block = stored[start:start + block_size]
        words = fir.filter_block(pack_samples(block, fir.samp_cls, params['mem_dwidth']),
                                 len(block), fir.taps, fir.carry, int(start == 0))
        raw = unpack_samples(words, len(block), fir.samp_cls, params['mem_dwidth'])
        # deserialize returns raw W-bit patterns, including unsigned negatives.
        # Reinterpret two's complement before applying the binary point.
        sign = 1 << (params['samp_w'] - 1)
        signed = (raw & (sign - 1)) - (raw & sign)
        result.append(signed.astype(float) * 2.0 ** (params['samp_i'] - params['samp_w']))
    return np.concatenate(result) if result else np.empty(0)


def bandlimited(nsamp: int, f_lo: float, f_hi: float, peak: float, seed: int) -> np.ndarray:
    """Periodic random-phase multisine, excluding DC and Nyquist, at a common peak."""
    freq = np.fft.rfftfreq(nsamp)
    band = (freq >= f_lo) & (freq <= f_hi) & (freq > 0) & (freq < 0.5)
    if not band.any():
        raise ValueError('measurement band contains no non-DC/non-Nyquist FFT bins')
    spectrum = np.zeros(len(freq), dtype=complex)
    spectrum[band] = np.exp(2j * np.pi * np.random.default_rng(seed).random(int(band.sum())))
    x = np.fft.irfft(spectrum, nsamp)
    return x * (peak / np.abs(x).max())


def frac_delay(x: np.ndarray, delay: float) -> np.ndarray:
    """Exact periodic fractional delay (measurement waveforms omit Nyquist)."""
    return np.fft.irfft(np.fft.rfft(x) * np.exp(-2j * np.pi * np.fft.rfftfreq(len(x)) * delay), len(x))


def evaluate(params: dict, evaluation: dict) -> dict:
    """Design and measure a FIR; return JSON-safe metrics and their measurement contract.

    ``input_peak`` is absolute and identical for both tests and all candidate designs.
    Warmup is periodic history, not zero padding; a full period is then measured.
    """
    from examples.dse_fir.fir_quality import (
        FirSpec,
        design_quantized,
        max_representable,
    )

    required = {'ntap', 'samp_w', 'samp_i', 'unroll_lane', 'mem_dwidth'}
    if set(params) != required:
        raise ValueError(f'params must contain exactly {sorted(required)}')
    for key in required - {'unroll_lane'}:
        if type(params[key]) is not int:
            raise ValueError(f'{key} must be an integer')
    if type(params['unroll_lane']) is not bool:
        raise ValueError('unroll_lane must be boolean')
    t, w, i, mem = (params[k] for k in ('ntap', 'samp_w', 'samp_i', 'mem_dwidth'))
    if not 2 <= t <= 4096 or not 2 <= w <= 32 or not 1 <= i <= w or not w <= mem <= 64:
        raise ValueError('invalid FIR dimensions or fixed-point format')
    cfg: dict = {"f_pass": 0.20, "f_stop": 0.28, "nsamp": 2048, "seed": 0,
                 "input_peak": 0.5, "nfft": 8192}
    if set(evaluation) - set(cfg):
        raise ValueError('unknown evaluation keys')
    cfg.update(evaluation)
    for key in ('nsamp', 'seed', 'nfft'):
        if type(cfg[key]) is not int or cfg[key] < 0:
            raise ValueError(f'{key} must be a nonnegative integer')
    n, nfft = cfg['nsamp'], cfg['nfft']
    if not max(16, t) <= n <= 1048576 or not 8 * t <= nfft <= 1048576:
        raise ValueError('require nsamp >= max(16, ntap), nfft >= 8*ntap, both <= 1048576')
    for key in ('f_pass', 'f_stop', 'input_peak'):
        if isinstance(cfg[key], bool) or not isinstance(cfg[key], (int, float)) or not np.isfinite(cfg[key]):
            raise ValueError(f'{key} must be finite numeric')
    spec = FirSpec(cfg['f_pass'], cfg['f_stop'])
    delta = 2.0 ** (i - w)
    if cfg['input_peak'] <= delta:
        raise ValueError('degenerate measurement: input_peak must exceed one input LSB')
    q = design_quantized(spec, t, w, i, input_peak=cfg['input_peak'])
    if not np.any(q.stored):
        raise ValueError('degenerate measurement: all taps quantized to zero')
    response = response_metrics(q.taps_real, spec, nfft)
    gain = response.pop('passband_gain')
    xp = bandlimited(n, 0, spec.f_pass, cfg['input_peak'], cfg['seed'])
    xs = bandlimited(n, spec.f_stop, 0.5, cfg['input_peak'], cfg['seed'] + 1)

    def steady(x):
        # ntap-1 prior samples give exactly the state of this infinite periodic input.
        return run_fixed(np.concatenate([x[-(t - 1):], x]), q.stored, params)[t - 1:]

    yp, ys = steady(xp), steady(xs)
    ref = frac_delay(xp, (t - 1) / 2)
    ref -= ref.mean()
    yp_ac = yp - yp.mean()
    fitted_gain = float(ref @ yp_ac / (ref @ ref))
    fitted = fitted_gain * ref
    sndr = (_db_ratio(float(fitted @ fitted), float((yp_ac - fitted) @ (yp_ac - fitted)))
            if fitted_gain > 0 else DB_FLOOR)
    bias = float(ys.mean())
    leakage = float(np.mean((ys - bias) ** 2))
    rejection = _db_ratio(float(np.mean(xs ** 2)) * gain ** 2, leakage)
    delta = 2.0 ** (i - w)
    limit = max_representable(w, i)
    l1 = float(np.abs(q.taps_real).sum())
    bound = (cfg['input_peak'] + delta) * l1
    return {
        **response, 'stopband_rej_db': rejection, 'passband_sndr_db': sndr,
        'dc_bias': bias, 'throughput_samp_per_cyc': float(mem // w if params['unroll_lane'] else 1),
        'metadata': {
            'evaluator_version': EVALUATOR_VERSION,
            'normalization': {'passband_gain': gain, 'passband_fitted_gain': fitted_gain,
                              'stopband': 'input power * mean passband magnitude squared / output AC power',
                              'sndr': 'positive least-squares delayed-input fit / residual AC power',
                              'db_floor': DB_FLOOR, 'db_ceiling': DB_CEILING,
                              'degenerate_signal': 'db_floor; zero noise with nonzero signal uses db_ceiling'},
            'measurement_scope': {'backend': 'FirCompute.filter_block Python golden; not RTL',
                                  'throughput': 'analytic steady-state compute only, no transport/startup overhead',
                                  'evaluation': cfg, 'warmup_samples': t - 1,
                                  'waveform': 'periodic random-phase multisine; DC/Nyquist excluded',
                                  'layer1': 'inclusive-band worst stopband bin / mean passband magnitude',
                                  'layer2': 'waveform-average; no ordering guarantee versus layer1',
                                  'passband_dc_bias': float(yp.mean())},
            'headroom': {'input_peak': cfg['input_peak'], 'input_quantization_margin': delta,
                         'quantized_tap_l1': l1, 'output_bound': bound,
                         'max_representable': limit, 'output_margin': limit - bound},
            'scale': q.scale,
            'taps': {'stored': q.stored.tolist(), 'real': q.taps_real.tolist(),
                     'r2': float(q.r2), 'n_clipped': q.n_clipped, 'n_zeroed': q.n_zeroed,
                     'method': q.method},
        },
    }
