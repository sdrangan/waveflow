---
title: What an equalizer does
parent: Block equalizer
nav_order: 1
snippets: run
---

# What an equalizer does

This page is for a reader who is not a wireless engineer: it says what the example computes and why that
formula, with one worked number. The full theory is linked at the end.

## A channel scales each symbol

A transmitter sends a sequence of **symbols** `s` -- complex numbers, each a point of a constellation
such as [QAM](https://en.wikipedia.org/wiki/Quadrature_amplitude_modulation). On the way to the receiver
the radio channel changes each symbol in two ways: it multiplies it by a complex **gain** `h`, which scales
its amplitude and rotates its phase, and it adds **noise** `w`:

$$
r = h\,s + w
$$

`h` is what the propagation did -- path loss, reflections adding up or cancelling
([fading](https://en.wikipedia.org/wiki/Fading)). `w` is random, with an average power (variance) `N`.
In a multicarrier system such as [OFDM](https://en.wikipedia.org/wiki/Orthogonal_frequency-division_multiplexing)
each subcarrier has its own `h`, and that one complex number describes the channel for every symbol on
it while the channel holds still.

The receiver does not choose `h` or `N`: it **estimates** them, usually from known pilot symbols. Those
estimates are this example's input; how they are made is not part of it.

## An equalizer undoes `h`

The receiver wants `s`, and has `r`. An [**equalizer**](https://en.wikipedia.org/wiki/Equalization_(communications))
multiplies `r` by a gain `g` chosen so that `z = g r` is as close to `s` as it can be. Everything
downstream -- deciding which constellation point was sent -- works on `z`.

The obvious choice is `g = 1/h`: then `z = s + w/h`, the channel is gone and only noise is left. This is
the [zero-forcing](https://en.wikipedia.org/wiki/Zero-forcing_equalizer) equalizer. Its trouble is the
`w/h`: when `h` is small -- a fade -- dividing by it multiplies the noise by a large number, and `z` is
mostly noise.

## The MMSE gain: the noise limits the gain

The example uses instead

$$
g = \frac{\overline{h}}{|h|^2 + N}
$$

where $\overline{h}$ is the complex conjugate of `h`. When the channel is strong (`|h|^2` much larger than
`N`) this is almost `1/h`, since $\overline{h} / |h|^2 = 1/h$. When the channel is weak, the `N` in the
denominator keeps `g` from growing: the equalizer refuses to amplify a symbol that is mostly noise. This
is the gain that makes the average of `|z - s|^2` smallest, for symbols of average power 1 -- the
**minimum mean square error** ([MMSE](https://en.wikipedia.org/wiki/Minimum_mean_square_error)) equalizer.

That average error has two parts, and the arithmetic needs nothing more: what `g` leaves of the channel,
`|g h - 1|^2`, and the noise it passes through, `|g|^2 N`. Here they are for both gains, with `g` from the
example's own golden ([`gain_golden`](golden.md#the-arithmetic)) -- first a deep fade, `h = 0.1` with
`N = 0.1`, then the strong channel of the [worked examples](golden.md#the-worked-examples), `h = 3 + 4j`
with `N = 0.25`:

```python
import numpy as np
from examples.block_eq.block_eq import gain_golden


def error(g, h, n):
    """The average of |g r - s|^2, for symbols of power 1: what g leaves of the channel, plus the noise it passes."""
    return abs(g * h - 1) ** 2 + abs(g) ** 2 * n


for h, n in [(0.1, 0.1), (3 + 4j, 0.25)]:
    gr, gi = gain_golden([np.real(h)], [np.imag(h)], [n])
    g = complex(gr[0], gi[0])
    print(f"h = {h}, N = {n}:  1/h = {1 / h:.4f} (error {error(1 / h, h, n):.4f}),"
          f"  g = {g:.4f} (error {error(g, h, n):.4f})")
```

```text
h = 0.1, N = 0.1:  1/h = 10.0000 (error 10.0000),  g = 0.9091-0.0000j (error 0.9091)
h = (3+4j), N = 0.25:  1/h = 0.1200-0.1600j (error 0.0100),  g = 0.1188-0.1584j (error 0.0099)
```

In the fade, `1/h = 10` turns noise of power 0.1 into noise of power 10 -- ten times the symbol's own
power -- while the MMSE gain, 0.9091, leaves an error below 1. On the strong channel the two gains differ
in the third digit, and so do their errors. (The `-0.0000j` is a real result: `g`'s imaginary part is
`-(0 * q)`, a negative zero, which the example keeps -- see [the golden](golden.md#signed-zeros).)

`N` must be positive: at `N = 0` the gain is `1/h` again, and at `h = 0` it is a division by zero. A noise
estimate of zero or less is not a measurement, so the example treats it as an error
([`BAD_NOISE`](protocol.md#errors-are-fatal)).

## One gain serves a row

A channel changes slowly next to the symbol rate: over its
[coherence time](https://en.wikipedia.org/wiki/Coherence_time_(communications_systems)) (and, across
frequency, its [coherence bandwidth](https://en.wikipedia.org/wiki/Coherence_bandwidth)) `h` is the same
for many consecutive symbols. So the receiver estimates `h` and `N` once for a group of symbols and uses
the same `g` for all of them. The example calls such a group a **row**: `m` symbols that saw the same
channel.

## What the example computes

A command carries `nrows` rows. For each row `i` it has the estimates `h[i]` and `N[i]`, and the row's `m`
received symbols `r[i, 0..m-1]`. The example computes

$$
g_i = \frac{\overline{h_i}}{|h_i|^2 + N_i}, \qquad z_{i,j} = g_i \, r_{i,j}
$$

-- one gain per row, then one complex multiply per symbol -- and returns the `z`, row by row. Written out
in real arithmetic, with `h = hr + j hi`, `g = gr + j gi`, `r = rr + j ri`:

```text
gr = hr / (hr*hr + hi*hi + N)          zr = gr*rr - gi*ri
gi = -hi / (hr*hr + hi*hi + N)         zi = gr*ri + gi*rr
```

The left column, once a row, is [`gain`](stages.md#gain); the right, once a symbol, is
[`apply`](stages.md#apply). That difference in rate is why the example is
[two stages](stages.md#why-two-stages). The exact order in which the float32 operations round is fixed by
[the golden](golden.md#the-arithmetic).

For the full theory -- where the MMSE gain comes from, and the matrix form a receiver with several
antennas uses -- see [Minimum mean square error](https://en.wikipedia.org/wiki/Minimum_mean_square_error)
and [Equalization](https://en.wikipedia.org/wiki/Equalization_(communications)); for the matrix form on
this platform, the [MIMO conjugate-gradient example](../mimo_cg/index.md).

Next, [The protocol](protocol.md): how the estimates and the symbols reach the hardware.
