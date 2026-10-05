"""Speed of the bit-exact models, measured against the float reference they shadow.

The models in this package trade speed for bits.  This module says how much, so the trade is a
number rather than a feeling -- and so a change that makes it worse is caught::

    python -m waveflow.vitis_l1.bench                 # the table
    python -m cProfile -s tottime -m waveflow.vitis_l1.bench    # where the time goes

Every row is normalised by ``np.fft`` on the **same machine and the same input**.  An absolute
"10 ms per frame" means nothing across laptops and CI runners; a ratio to a fixed compiled
reference travels.  ``tests/vitis_l1/fft/test_speed.py`` gates on that ratio.

Two things each row reports, because they answer different questions:

* ``ratio`` -- the **cost** of bit-exactness.  The model makes dozens of int64 passes per stage
  (multiply, align, wrap, narrow -- once per partial product), where ``np.fft`` makes one compiled
  float pass, so roughly 10^3 is inherent.  The per-butterfly implementation this replaced sat
  near 10^5, which is what the gate exists to keep out.
* ``lsb_rms`` / ``lsb_max`` -- what you would **lose** by using rounded ``np.fft`` instead.  Even
  with no overflow anywhere, the hardware rounds the twiddles to ``tw_w`` bits and truncates every
  partial product of every inter-stage rotation, so the two disagree by several LSBs and the gap
  grows with ``L``.  A fast non-bit-exact mode would have to be judged against this column.
"""
from __future__ import annotations

import timeit
from dataclasses import dataclass

import numpy as np

from . import fft


@dataclass(frozen=True)
class FftBenchRow:
    """One length's measurement.  Times are per frame, best of ``repeat``."""

    length: int
    model_s: float      # bit-exact model, seconds per frame
    npfft_s: float      # np.fft on the same input, seconds per frame
    lsb_rms: float      # rounded np.fft vs the model, in output LSBs
    lsb_max: float

    @property
    def ratio(self) -> float:
        return self.model_s / self.npfft_s


def _per_call(fn, budget_s: float, repeat: int) -> float:
    """Best-of-``repeat`` seconds per call, with enough calls per repeat to fill ``budget_s``."""
    t1 = timeit.timeit(fn, number=1)
    number = max(1, int(budget_s / max(t1, 1e-9)))
    return min(timeit.repeat(fn, number=number, repeat=repeat)) / number


def bench_fft(lengths=(64, 256, 1024, 4096), in_w: int = 16, in_i: int = 2,
              tw_w: int = 18, tw_i: int = 2, *, seed: int = 0, budget_s: float = 0.05,
              repeat: int = 3) -> list[FftBenchRow]:
    """Time ``fft_general`` (``NO_SCALING``) against ``np.fft`` at each length.

    The input is full-scale random stored integers.  ``np.fft`` sees the same integers as a
    complex vector; because ``NO_SCALING`` keeps the fractional width (the output format only grows
    integer bits), its rounded result is directly comparable to the model's stored output.
    """
    rng = np.random.default_rng(seed)
    lim = 1 << (in_w - 1)
    rows = []
    for length in lengths:
        x_re = rng.integers(-lim, lim, length)
        x_im = rng.integers(-lim, lim, length)
        z = x_re + 1j * x_im

        def model():
            return fft.fft_general(x_re, x_im, length, in_w, in_i, tw_w, tw_i)

        y_re, y_im, fo = model()
        if fo.W - fo.int_bits != in_w - in_i:
            raise AssertionError(
                f"L={length}: output has {fo.W - fo.int_bits} fractional bits, input "
                f"{in_w - in_i}; the np.fft comparison assumes they match")
        ref = np.fft.fft(z)
        err = np.concatenate([y_re - np.round(ref.real), y_im - np.round(ref.imag)])

        rows.append(FftBenchRow(
            length=length,
            model_s=_per_call(model, budget_s, repeat),
            npfft_s=_per_call(lambda: np.fft.fft(z), budget_s, repeat),
            lsb_rms=float(np.sqrt(np.mean(err.astype(np.float64) ** 2))),
            lsb_max=float(np.max(np.abs(err)))))
    return rows


def format_fft_table(rows: list[FftBenchRow]) -> str:
    lines = [f"{'L':>6}  {'model':>10}  {'np.fft':>10}  {'ratio':>7}  {'lsb rms':>8}  {'lsb max':>8}"]
    for r in rows:
        lines.append(f"{r.length:>6}  {r.model_s * 1e3:>7.2f} ms  {r.npfft_s * 1e6:>7.1f} us  "
                     f"{r.ratio:>6.0f}x  {r.lsb_rms:>8.2f}  {r.lsb_max:>8.0f}")
    return "\n".join(lines)


def main() -> None:
    print("Vitis L1 SSR FFT, R=4, NO_SCALING, ap_fixed<16,2> in, <18,2> twiddles")
    print(format_fft_table(bench_fft()))


if __name__ == "__main__":
    main()
