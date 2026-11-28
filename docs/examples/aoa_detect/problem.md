---
title: The problem
parent: Emitter detection and AoA
nav_order: 1
summary: "The problem statement. C channels, n_rx antennas, a block of B samples per channel arriving every B/fs seconds for every channel at once. In each channel an emitter is absent (covariance I) or present at a fixed angle and SNR (I + γ u uᴴ), switching on and off as a two-state Markov process. The receiver detects each emitter, estimates its AoA and SNR, and declares when it stops. What is measured, kept as separate metrics for now: from a result message for every decision, the decision time (from the block's arrival to the message), missed detections, false alarms, early stops, and the angular error. What can be designed: block size, thresholds, the direction grid, the shared hardware and memory, where each stage runs, and the policies."
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
  $u(\theta)^H Q\, u(\theta) = 1 + \gamma$ in the emitter's direction.
- **The array is a uniform rectangular array (URA)** of $n_{rx} = n_h \times n_v$ elements at
  half-wavelength spacing, in a vertical plane facing the sector's boresight. A direction is a pair
  $\theta = (\phi, \varepsilon)$, azimuth and elevation, and the element in column $m$ and row $n$ has

  $$
  u_{m,n}(\theta) = \frac{1}{\sqrt{n_{rx}}}\, e^{\,j \pi \left( m \cos\varepsilon \sin\phi \,+\, n \sin\varepsilon \right)} .
  $$

  The elements are directional (a sector pattern), which enters through the SNR
  ([The workload](workload.md)).
- The emitter's signal is complex Gaussian and flat across the channel, so the samples of a block are
  independent given $Q$.

## The emitters

Each channel's emitter switches **on and off as a two-state Markov (Gilbert–Elliott) process**, stepped
once per block:

- **Off → on** with probability $p_{on}$. At each onset a new emitter is placed: its direction $\theta$
  and SNR $\gamma$ come from its position and power ([The workload](workload.md)). Both stay
  **constant while the emitter is on**.
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

**A result message for every decision.** Each time the system decides that an emitter has arrived in a
channel, or that it has departed, it emits a **result message**: the channel, the block the decision
came from, the decision (arrival or departure), and, for an arrival, $\hat{\theta}$ and $\hat{\gamma}$
([The architecture](arch.md#the-result-message)). Everything below is measured from these messages
against the emitters' true on/off times and angles.

**Decision time.** For each decision, from the **arrival of the block** it came from (the instant
$t_n$ at which block $n$ is complete) to the **result message**. This is the response time of the
hardware: waiting for a free unit, the read contended at the memory, the computation and the return.
Its distribution, mean and tail, is the timing metric.

**Accuracy**, by matching each emitter's on-period to the decisions in its channel:

- **Missed detection:** an emitter for which no arrival decision is made while it is on.
- **False alarm:** an arrival decision in a channel with no emitter on at that block, per channel per
  second.
- **Early stop:** a departure decision while the emitter is still on, per channel per second.
- **Angular error:** $|\hat{\theta} - \theta|$ at each correct arrival decision; also the SNR error
  $\hat{\gamma} - \gamma$.

**The metrics are kept separate for now.** How to combine them into one objective for optimization (for
example, missed detections counting an arrival later than a delay bound $D_{max}$ as missed) is decided
once simulations show how they trade.

**Also recorded:** each emitter's **detection delay** from its onset to its arrival decision (the
decision time plus every block that passed before a decision: blocks before the gate fired, dropped
notifications, `pending` blocks and late reads), and the same for its departure; notifications
dropped and reads late per channel; resources (DSPs, LUTs, BRAM).

Two kinds of quantity come out of these, and they need different simulations:

- **Accuracy per block** (false alarms, detection per block, the angular and SNR errors) follows from
  the thresholds, $B$ and the direction grid. The **untimed model** computes it.
- **Decision time**, and every quantity it changes (the detection delay, dropped notifications
  and late reads), depends on when each job runs on the shared
  hardware: queueing behind other channels' jobs and contention for the memory. **Only a timed
  simulation** gives it.

## The objective

The metrics are **reported separately** (above). For the design-space exploration they are brought
together **under a resource budget**, in one of two forms:

- **the cheapest design that meets targets:** minimize resources (DSPs, LUTs, BRAM and URAM, weighted
  or each capped) subject to a percentile of the decision time, the missed-detection rate, the false
  alarm and early-stop rates, and the angular error;
- **the best design within a budget:** with the resources capped, the best of the metrics, in an order
  or weighting to be chosen.

The targets, the budget and which form are set **after the first simulations**, once it is clear how
the metrics trade. What matters now is that no design knob can simply be maxed out: every unit, every
bit of a stored format and every slot of a buffer comes out of the same budget.

Three models score a design, and [The parameters](param.md) says which each parameter needs:

- the **bit-exact golden**, untimed, for the accuracy metrics;
- the **cost model**, for the resources;
- the **timed simulation**, for the decision time, and for the drops and late reads that turn into
  misses and delays.

Several parameters need more than one, and those make the problem
([where the models meet](param.md#where-the-models-meet)).

## What can be designed

- **The algorithm's parameters:** the block size $B$, the thresholds, and the direction grid's size
  $L$ ([The algorithm](algo.md)).
- **The hardware:** the systolic unit's size and how many there are, the vector units, the memory's
  width and depth, the credits, where the covariance matrices are kept.
- **The number formats:** the input's width, and each stored value's width and overflow mode
  ([The parameters](param.md#number-formats)).
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

- **Sizes and formats:** proposed on [The parameters](param.md).
- **The emitter process and the geometry:** proposed on [The workload](workload.md).
- **How to combine the metrics** into one objective (a delay bound $D_{max}$, an AoA tolerance, weights), after the first simulations.
