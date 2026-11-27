---
title: The problem
parent: Emitter detection and AoA
nav_order: 1
summary: "The problem statement. C channels, n_rx antennas, a block of B samples per channel arriving every B/fs seconds for every channel at once. In each channel an emitter is absent (covariance I) or present at a fixed angle and SNR (I + γ u uᴴ), switching on and off as a two-state Markov process. The receiver detects each emitter, estimates its AoA and SNR, and declares when it stops. What is measured: the detection and stop delays, the detection probability within a delay bound, false alarms, the AoA and SNR errors, blocks skipped under overload, and resources. What can be designed: block size, thresholds, the direction grid, the shared hardware and memory, where each stage runs, and the policies."
---

# The problem

**Status: under construction.** This page states the problem. The sizes are proposals, and several
choices are open (listed at the end).

## The signal

A receiver has $n_{rx}$ antennas and watches $C$ frequency channels, each sampled at $f_s$. The
samples are

$$
x[k, i, j], \qquad k \text{ the sample index},\quad i = 0, \dots, n_{rx}-1 \text{ the antenna},\quad j = 0, \dots, C-1 \text{ the channel}.
$$

Write $x_{k,j} \in \mathbb{C}^{n_{rx}}$ for the vector across antennas. Its spatial covariance is

$$
Q_{k,j} = \mathbb{E}\left[ x_{k,j}\, x_{k,j}^H \right] .
$$

At any time a channel holds **no emitter or one**:

$$
Q_{k,j} =
\begin{cases}
I & \text{no emitter,} \\
I + \gamma_{k,j}\, u(\theta_{k,j})\, u(\theta_{k,j})^H & \text{an emitter at angle } \theta_{k,j} \text{ with SNR } \gamma_{k,j}.
\end{cases}
$$

- The noise is white and normalized to power 1 on each antenna. Its level is assumed calibrated
  beforehand.
- $u(\theta)$ is the array's spatial signature, normalized to $\lVert u(\theta) \rVert = 1$, so
  $\gamma$ is the **array SNR** (the per-antenna SNR is $\gamma / n_{rx}$) and
  $u(\theta)^H Q\, u(\theta) = 1 + \gamma$ in the emitter's direction. For a uniform linear array at
  half-wavelength spacing,

  $$
  u_i(\theta) = \frac{1}{\sqrt{n_{rx}}}\, e^{\,j \pi i \sin\theta} .
  $$

- The emitter's signal is complex Gaussian and flat across the channel, so the samples of a block are
  independent given $Q$.

## The emitters

Each channel's emitter switches **on and off as a two-state Markov (Gilbert–Elliott) process**, stepped
once per block:

- **Off → on** with probability $p_{on}$. At each onset a new angle $\theta$ is drawn uniformly in
  $[-\theta_{max}, \theta_{max}]$ and a new SNR $\gamma$ from a range in dB. Both stay **constant while
  the emitter is on**.
- **On → off** with probability $p_{off}$, so an emitter lasts $1 / p_{off}$ blocks on average.

The channels are independent by default. A **burst** option turns several channels on at the same
block (a common trigger), which is the hardest case for shared hardware.

## Blocks, and the instant they arrive

The receiver works on **blocks of $B$ samples**. Block $n$ of every channel is complete at the same
instant,

$$
t_n = (n + 1)\, \frac{B}{f_s} ,
$$

so the controller has up to $C$ blocks to act on at once, every $B / f_s$ seconds. How each block is
processed (search or track) is the [algorithm](algo.md); how the blocks reach the hardware and the
results reach the host is the interface (page to come).

## What is measured

For each emitter, with onset at the start of block $n_0$ and end at the start of block $n_1$:

- **Detection delay:** from the onset to the moment its AoA and SNR estimates are available to the
  host. This includes waiting for blocks to complete, queueing for shared hardware, and the
  computation.
- **Stop delay:** from the end to the moment the host is told it has stopped.
- **Detection probability within a delay bound** $D_{max}$: the fraction of emitters detected, with
  the AoA within a tolerance, no later than $D_{max}$ after their onset. **This is the headline
  metric.**
- **False alarms:** detections declared in a channel holding no emitter, per channel per second.
- **AoA and SNR errors** of the detections.
- **Blocks skipped** under overload (by the late-block policy, below), per channel.
- **Resources:** DSPs, LUTs, BRAM, and the CPU's load.

Two kinds of quantity come out of these, and they need different simulations:

- **Accuracy per block** (false alarms, detection per block, the AoA and SNR errors) follows from the
  thresholds, $B$ and the direction grid. The **untimed model** computes it.
- **Delay**, and every quantity it changes (detection within $D_{max}$, skipped blocks, the stop
  delay), depends on when each job runs on the shared hardware: queueing behind other channels' jobs
  and contention for the memory. **Only a timed simulation** gives it.

## What can be designed

- **The algorithm's parameters:** the block size $B$, the thresholds, and the direction grid's size
  $L$ ([The algorithm](algo.md)).
- **The hardware:** the systolic unit's size and how many there are, the memory's width and banks,
  where the covariance matrices are kept (shared memory or inside the unit).
- **Where each stage runs:** the energy checks and the tracking check in hardware or on the CPU; the
  control on the CPU.
- **The policies:**
  - the **priority** of tracking checks over searches;
  - **late blocks:** what happens when a channel's search for block $n$ has not finished when block
    $n+1$ needs one (queue it, drop the older one, or skip the newer one);
  - a cap on searches in flight.

The trade is non-monotone: a smaller $B$ or lower thresholds react faster per block but raise the
search load, which adds queueing delay and, under overload, skipped blocks.

## Out of scope

- More than one emitter in a channel at a time.
- Moving emitters: the angle is fixed while the emitter is on.
- Estimating the noise level: it is calibrated beforehand.
- Frequency-selective channels.

## Open

- **Sizes** (proposed): $n_{rx} = 8$, $C = 16$, $B = 256$, $L = 64$, $f_s$ per channel to be chosen so
  that the systolic unit is loaded but not saturated on average.
- **The emitter process:** $p_{on}$, $p_{off}$, the SNR range, $\theta_{max}$, and how often bursts
  occur.
- **$D_{max}$** and the AoA tolerance.
- **Fixed point:** the sample, covariance and scan formats.
