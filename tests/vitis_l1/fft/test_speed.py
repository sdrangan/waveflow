"""The bit-exact FFT model must stay stage-vectorized.

Gated on the ratio to ``np.fft`` on the same machine, not on wall time, so it holds on any
runner.  Today the ratio is ~10^3 at ``L=1024``; the per-butterfly loop it replaced was ~10^5.
The threshold sits between them with headroom for a slow or noisy machine.
"""
from __future__ import annotations

from waveflow.vitis_l1.bench import bench_fft

MAX_RATIO = 10_000


def test_fft_general_stays_vectorized():
    (row,) = bench_fft((1024,), budget_s=0.02)
    assert row.ratio < MAX_RATIO, (
        f"fft_general at L=1024 is {row.ratio:.0f}x np.fft (limit {MAX_RATIO}x, ~10^3 expected): "
        f"{row.model_s * 1e3:.1f} ms/frame. Has a per-butterfly loop crept back in? "
        f"Profile with: python -m cProfile -s tottime -m waveflow.vitis_l1.bench")


def test_np_fft_is_not_bit_exact():
    """Pins *why* the slow model exists: rounded np.fft misses by several LSBs even with no
    overflow, because the hardware rounds its twiddles and truncates every rotation product."""
    (row,) = bench_fft((256,), budget_s=0.001, repeat=1)
    assert row.lsb_max > 1
