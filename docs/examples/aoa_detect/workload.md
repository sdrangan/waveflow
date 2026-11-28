---
title: The workload
parent: Emitter detection and AoA
nav_order: 4
summary: "How the receiver is driven in a test. Emitters are placed on the ground, uniformly over the area of one sector, and their SNR follows from free-space path loss (Friis), their transmit power, a 3GPP sector element pattern and the receiver's noise; their direction (azimuth, elevation) from the geometry. Each channel's emitter switches on and off as a Gilbert–Elliott process with an optional burst trigger. A statistical mode (SNR uniform in dB, direction uniform in the sector) is kept as a check. The source generates every channel's samples block by block from a seed, with a truth log of every emitter's on/off blocks, direction and SNR; the same samples drive pysim and, replayed from a file, the RTL. Named scenarios cover idle noise, one emitter, crossings of the thresholds, bursts and long random runs. Ray-traced channels are the later step."
---

# The workload

**Status: under construction.** The model is fixed; the values are proposals (also listed on
[The parameters](param.md)).

The workload is everything a test feeds the receiver: where the emitters are, when they are on, and
the samples that result. It has to be **reproducible** (a seed gives the same samples in pysim and at
RTL) and come with a **truth log**, since every metric of [the problem](problem.md#what-is-measured) is
a comparison of the result messages with it.

## The geometry: emitters on the ground

The default places emitters **on the ground, in one sector**, and derives their direction and SNR from
the geometry. This gives realistic joint statistics (far emitters are both weaker and closer to the
horizon), and it is the same description a ray tracer takes, so moving to ray-traced channels later
changes the channel, not the workload.

- **The receiver** is a URA of $n_{rx} = n_h \times n_v$ elements ([the signal](problem.md#the-signal)) at
  height $h_{rx}$, facing the sector's boresight, tilted down by $\delta$.
- **The sector** is the ground between ranges $r_{min}$ and $r_{max}$ and azimuths
  $[-\phi_{max}, \phi_{max}]$ about boresight.
- **An emitter** is placed **uniformly over that area** (so its range has density proportional to $r$),
  at height $h_e$, with a transmit power $P_t$ drawn uniformly in dBm.

**Direction.** From the receiver to the emitter: azimuth $\phi$ about boresight, and elevation
$\varepsilon = -\arctan\!\big((h_{rx} - h_e) / r\big)$ (below the horizon).

**Per-element SNR**, in dB:

$$
\gamma_{el} = P_t - \mathrm{FSPL}(d) + G(\phi, \varepsilon) - N, \qquad
\mathrm{FSPL}(d) = 20 \log_{10}\!\frac{4 \pi d}{\lambda}, \qquad
N = -174 + 10 \log_{10} f_s + F ,
$$

with $d$ the distance, $\lambda$ the wavelength, $F$ the receiver's noise figure, and $G$ the element's
gain: the 3GPP TR 38.901 sector element, 8 dBi at its peak,

$$
G = 8 - \min\!\Big( 12 \big(\tfrac{\phi}{65^\circ}\big)^2 + 12 \big(\tfrac{\varepsilon + \delta}{65^\circ}\big)^2,\ 30 \Big) .
$$

The **array SNR** of the model is $\gamma = n_{rx}\, \gamma_{el}$ (in linear terms).

**Proposed values**, at a carrier of 3.5 GHz and $f_s = 30.72$ MHz per channel ($N = -92$ dBm with
$F = 7$ dB):

| | Value |
|---|---|
| $h_{rx}$, $\delta$ | 25 m, 6° down |
| $r_{min}$, $r_{max}$ | 100 m, 2 km |
| $\phi_{max}$ | 60° |
| $h_e$ | 1.5 m |
| $P_t$ | uniform in 5 to 25 dBm |

With these (the 5 dB of extra power offsets the wider channel's 5 dB of extra noise), the per-element
SNR is between about **−6 and 19 dB for 90% of emitters** (median 6 dB),
with a few near the receiver up to about 40 dB; the elevation is between about 0.7° and 13° below the
horizon. (From $2 \times 10^5$ draws of the model above.) The strong tail matters: it sets the input
format's headroom ([below](#the-samples)).

**A statistical mode** is kept as a check and for sweeps: the per-element SNR uniform in dB over
$[-6, 30]$, the azimuth uniform in $[-\phi_{max}, \phi_{max}]$, the elevation uniform over the sector's
range. It decouples SNR from direction, which the geometric mode does not.

## When emitters are on

Each channel's emitter follows a **Gilbert–Elliott process**, stepped once per block:

- **off → on** with probability $p_{on}$: a new emitter is placed (above) and stays fixed while on;
- **on → off** with probability $p_{off}$.

An emitter lasts $1 / p_{off}$ blocks on average, and a channel is busy a fraction
$p_{on} / (p_{on} + p_{off})$ of the time. The channels are independent, except for the **burst
trigger**: with probability $p_{burst}$ per block, $k_{burst}$ channels chosen at random that are off
turn on together. It is the worst case for the shared hardware, as after an event many emitters
appear at once.

**Proposed values**, at $B = 256$ and $f_s = 30.72$ MHz (a block every 8.33 µs), chosen for about 1.5
activations a block over $C = 8$ channels ([the load](param.md#what-they-imply)):

| | Value | Meaning |
|---|---|---|
| $p_{off}$ | $3/16$ | emitters last about 5 blocks, 44 µs |
| $p_{on}$ | $3/16$ | a channel is busy half the time |
| $p_{burst}$ | $1/50$ | a burst about every 50 blocks, 0.4 ms |
| $k_{burst}$ | 4 | half the channels |

## The samples

For channel $j$ and sample $k$ of a block:

$$
x_{k,j} = \sqrt{\gamma_{el}}\; a(\theta_j)\, s_{k,j} + w_{k,j}, \qquad s_{k,j} \sim \mathcal{CN}(0, 1), \quad w_{k,j} \sim \mathcal{CN}(0, I),
$$

with $a(\theta) = \sqrt{n_{rx}}\, u(\theta)$ the unit-modulus steering vector, and the first term absent
while the channel's emitter is off. This is exactly the covariance of [the signal](problem.md#the-signal).

**Quantization: the input gain.** The input gain is fixed (calibrated, not an automatic gain
control): it scales the samples so that the thermal noise has a complex standard deviation of
$\sigma$ LSBs, then they are rounded and saturated to $b$ bits per real and imaginary part. With
1 LSB steps, rounding adds noise of power $1/6$ LSB² per complex sample, and an emitter of
per-element SNR $\gamma_{el}$ has a per-part standard deviation of $\sigma \sqrt{(1 + \gamma_{el})/2}$
LSBs. So $\sigma$ trades two things:

- **Rounding noise against thermal noise:** $10 \log_{10}\big(1 / (6 \sigma^2)\big)$ dB. At
  $\sigma = 4$ LSBs it is −19.8 dB: it raises the noise floor by under 0.05 dB.
- **Headroom:** the largest per-element SNR whose per-part standard deviation stays within a quarter of
  full scale (clipping only beyond four standard deviations), about $6.02\, b - 20 \log_{10} \sigma - 9$
  dB.

| $b$ | $\sigma = 1$ | $\sigma = 2$ | $\sigma = 4$ | $\sigma = 8$ |
|---|---|---|---|---|
| rounding vs thermal | −7.8 dB | −13.8 dB | −19.8 dB | −25.8 dB |
| headroom at 16 bits | 81 dB | 75 dB | 69 dB | 63 dB |
| headroom at 12 bits | 57 dB | 51 dB | 45 dB | 39 dB |
| headroom at 8 bits | 33 dB | 27 dB | 21 dB | 15 dB |

**Proposed: $\sigma = 4$ LSBs**, the noise at −81 dBFS at 16 bits. Rounding is then negligible, and
the headroom covers the workload's strong tail (about 40 dB per element) at 16 and 12 bits; at 8 bits
it does not, which is part of what the input format trades. The golden's thresholds use the calibrated
noise power $N_0 = \sigma^2 + 1/6$ LSB². Clipped samples are counted, per channel.

**Word layout.** Each word of the sample stream holds one channel's antennas for one sample, the
layout [the ring](arch.md#the-ring) needs; a block is $C \cdot B$ of them, channel after channel within
each sample index.

## The source and the truth log

One source module generates the stream, block by block, from a seed:

- **In pysim**, it is the sample-stream source in front of the capture and power detector.
- **At RTL**, the same samples are written to a file beforehand and replayed by the testbench's
  stream driver, as the RF examples do with `RfFileSource` ([the rt_sounder example](../rt_sounder/index.md)).
  Identical input in both backends is what makes their result messages comparable.

Alongside the samples it writes the **truth log**: for every emitter, its channel, its first and last
block, its direction and its SNR. Scoring matches the result messages against it.

## Scenarios

| Scenario | What it drives | What it checks |
|---|---|---|
| `idle` | noise only, every channel | false alarms; that the power detector and the controller stay quiet |
| `one` | one strong emitter in one channel, on then off | an arrival and a departure, the direction, the decision time with no load |
| `edge` | one emitter near each threshold | misses, early stops, repeated searches of a weak emitter |
| `burst` | $k_{burst}$ channels turned on in one block | queueing at the units, dropped notifications, late reads |
| `random` | the Gilbert–Elliott process over many blocks | every metric's distribution |

`one` and `edge` are short and deterministic, for the RTL gates; `burst` and `random` are long, for
pysim, with their length set by the tail the metrics need.

## Later: ray-traced channels

Waveflow's ray-traced RF environment (`waveflow.rfenv`, with Sionna RT) can replace free space with a
scene: the same emitter positions, the channel from every path. With multipath an emitter is no longer
a single direction, so the covariance has rank above one, the scan's peak spreads, and the angular
error grows. That is a harder and more realistic test of the same architecture, for after the
free-space runs.

## Open

- The values above, and the carrier and bandwidth they assume.
- Whether an emitter's power or position may change while it is on.
