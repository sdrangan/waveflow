---
title: The parameters
parent: Emitter detection and AoA
nav_order: 5
summary: "Every parameter of the design in one place, with a proposed value, whether the exploration varies it, and what scores it: the bit-exact golden (accuracy), the cost model (resources) or the timed simulation (timing). The stored values (Q̂, the scan's powers, uᴴX, the departure statistic) are narrower than exact, each with a width and an overflow mode. Several parameters are scored by more than one model: the input format sets both accuracy and the read time, every format sets resources and so how many units fit a budget, and the block length, the thresholds and the credits trade accuracy, load and delay. Then the quantities they imply: a job is read-bound at the shared port, the average load is small, and bursts set the design."
---

# The parameters

**Status: under construction.** Every value is a **proposal**. The formats are first guesses, to be
settled by the bit-exact model against floating point.

**Explored** marks what the design-space exploration varies; the rest is fixed for a study.
**Scored by** says which model is needed to see a parameter's effect:

- **A**, accuracy: the bit-exact golden, untimed (false alarms, misses per block, early stops, the
  angular and SNR errors);
- **R**, resources: the cost model (DSPs, LUTs, BRAM, URAM);
- **T**, timing: the timed simulation (the decision time, dropped notifications, late reads, and the
  misses and delays they cause).

A parameter scored by one model can be optimized with that model alone. **The parameters scored by
more than one couple the models, and they are what makes the exploration hard**
([why](#where-the-models-meet)).

## Platform

| Parameter | Proposed | Explored |
|---|---|---|
| Part | `xczu48dr` (RFSoC) | no |
| Clock | 250 MHz | no |
| Tools | Vitis / Vivado 2025.1 | no |
| CPU | A53, the calibrated `a53_hpi_1200mhz_gem5v25_1_vitis2025_1` platform | no |

## Workload

On [The workload](workload.md).

| Parameter | Symbol | Proposed | Explored |
|---|---|---|---|
| Carrier | $f_c$ | 3.5 GHz | no |
| Sample rate per channel | $f_s$ | 10 MHz | no |
| Noise figure | $F$ | 7 dB | no |
| Receiver height, downtilt | $h_{rx}$, $\delta$ | 25 m, 6° | no |
| Sector: ranges, half-width | $r_{min}$, $r_{max}$, $\phi_{max}$ | 100 m, 2 km, 60° | no |
| Emitter height, power | $h_e$, $P_t$ | 1.5 m, uniform 0 to 20 dBm | no |
| On and off probabilities per block | $p_{on}$, $p_{off}$ | 1/1600, 1/400 | as a load sweep |
| Burst probability per block, size | $p_{burst}$, $k_{burst}$ | 1/4000, 8 | as a load sweep |
| Seed, length | | per scenario | no |

## Algorithm

On [The algorithm](algo.md).

| Parameter | Symbol | Proposed | Explored | Scored by |
|---|---|---|---|---|
| Array | $n_h \times n_v$, $n_{rx}$ | 4 × 2, 8 | no | |
| Channels | $C$ | 16 | no | |
| Block length (samples) | $B$ | 256 | yes | A, R, T |
| Direction grid | $L_\phi \times L_\varepsilon$, $L$ | 16 × 4, 64 | yes | A, R, T |
| Search gate (a.1) | $\tau_e$ | from a false-alarm probability per block, $10^{-3}$ | yes | A, T |
| Detection (a.4) | $\tau_p$ | from a false-alarm probability per search, $10^{-4}$ | yes | A |
| Tracking energy (b.1) | $\tau_e'$ | so that a present emitter rarely triggers b.2 | yes | A, T |
| Stop (b.2) | $\tau_\gamma$ | from an early-stop probability per check, $10^{-4}$ | yes | A |

The thresholds are stored as numbers, pre-scaled to what each comparison sees, computed by the host
from these targets with the chi-square statistics on [the algorithm](algo.md).

## Number formats

Widths are for each of the real and imaginary parts of a complex value. **Accumulators are exact**;
**stored values are narrower than exact**, and each has an **overflow mode**: saturate (clip to the
largest value) or wrap. The golden is bit-exact in both, so overflow is an accuracy effect it scores.
Saturation is the expected choice; wrap is kept to show what it costs.

| Value | Where | Accumulated in (exact) | Stored as | Mode | Explored | Scored by |
|---|---|---|---|---|---|---|
| Input sample $x$ | stream, ring | -- | 16 bits (Q1.15); 12 and 8 bits packed | saturate at the input | yes | A, R, **T** |
| Energy sum | power detector | 44 bits ($B\, n_{rx}$ terms of 32 bits) | compared directly | -- | no | |
| Gram matrix $\hat{Q}$ | systolic unit | 41 bits | **18 bits**, after the $1/B$ shift | saturate | yes | A, R |
| Steering vectors $U$ | constant table | -- | 16 bits | -- | yes | A, R |
| Scan powers $p_\ell$ | systolic unit | 43 bits | **24 bits** | saturate | yes | A, R |
| Departure projection $u^H X$ | vector unit | 36 bits | **18 bits** | saturate | yes | A, R |
| Departure statistic $p_b$ | vector unit | 44 bits | **24 bits** | saturate | yes | A, R |
| Thresholds | registers | -- | as the value each compares against | -- | follows | |

Why these stored widths: 18 bits fits a DSP's narrow input (the xczu48dr's DSP48E2 multiplies 27 × 18 bits), so
a wider $\hat{Q}$, $U$ or $u^H X$ costs a second DSP per multiply; 24 bits keeps a power's dynamic
range over the workload's 45 dB of SNR. Where the strong tail of the workload ([up to 40 dB per
element](workload.md#the-geometry-emitters-on-the-ground)) saturates $\hat{Q}$, the scan's peak
flattens and the angular error grows. That is the trade the golden measures.

**The input format reaches the timing.** A ring word read at port A holds 4 samples at 16 bits, 5 at
12 bits packed, 8 at 8 bits. Jobs are read-bound ([below](#what-they-imply)), so a narrower input
shortens every read and every decision, at a cost in accuracy and in the strong tail's clipping.

## Memory and queues

| Parameter | Symbol | Proposed | Explored | Scored by |
|---|---|---|---|---|
| Ring depth (slots) | $R$ | 8 | yes | R, T |
| Ring memory | | URAM, true dual port | no | |
| Ring port B (capture) width | | one sample word a cycle | follows | |
| Ring port A (readers) width | | 128 bits | yes | R, T |
| Crossbar data width, read burst | | 128 bits, 64 beats | yes | R, T |
| State-message queue depth | | $C$ (every channel answered at once) | no | |
| Result-message queue depth | | 64 | no | |
| Summary period | $P$ | 1,000 blocks (25.6 ms) | no | |

## Credits

| Parameter | Proposed | Explored | Scored by |
|---|---|---|---|
| Notification credits (detector → controller) | 16 | yes | T |
| Dispatch credits per unit (controller → a unit) | 2 | yes | T |

## Units

| Parameter | Symbol | Proposed | Explored | Scored by |
|---|---|---|---|---|
| Systolic units | $N_s$ | 1, 2, 4 | yes | R, T |
| Systolic array | $R_s \times C_s$ | 8 × 8 (one Gram output per cell at $n_{rx} = 8$); 4 × 4 | yes | R, T |
| Vector units | $N_v$ | 1, 2 | yes | R, T |
| Vector lanes | $V$ | 8 ($= n_{rx}$) | no | |

## Policies

| Policy | Options | Proposed | Scored by |
|---|---|---|---|
| Notification order when credits are short | fixed channel order, departures first, round-robin, highest energy first | departures first, then round-robin | T |
| A late read | no decision; the channel re-notifies on its next block | fixed | |

## Where the models meet

Taken alone, each half is easy. The formats' accuracy is a sweep of the golden. The timed half, the
units, the port, the ring and the credits, is monotone: each one added shortens the delay, so an
explorer would add them up to the budget and confirm. Four couplings make it a real problem:

1. **The input format sets the read time** (above): one format, scored by accuracy and timing.
2. **Formats set resources, and resources set how many units fit.** A stored width past a DSP's
   input doubles the DSPs per multiply. Under a **fixed resource budget**, a narrower $\hat{Q}$ or $U$
   can buy another systolic unit; whether that helps depends on whether the delay is limited by the
   units or by port A.
3. **$B$ and the thresholds are not monotone, and cross from accuracy to timing.** A longer block is
   more accurate per block but gives fewer, longer jobs and a longer wait for each block. A lower
   search gate catches weak emitters sooner, but its false alarms load the units and slow every
   channel's decision, to the point of drops and late reads that turn into misses.
4. **Credits against the ring's depth are not monotone.** More notification credits drop fewer
   notifications but queue more work; with $R$ fixed, queued jobs read older blocks, and more of them
   come back late. Where the turn is depends on how long jobs really take while they compete.

The objective that brings these together is on [the problem](problem.md#the-objective).

## What they imply

At the proposed values:

| Quantity | Value |
|---|---|
| Block period, $B / f_s$ | 25.6 µs, 6,400 cycles at 250 MHz |
| One channel's block in the ring | $B\, n_{rx} = 2048$ samples, 8 KB |
| One slot (all channels) and the ring | 128 KB; $R = 8$ is 1 MB (URAM) |
| A job's read at port A, alone | 2048 samples at 4 a beat: 512 cycles, about 2 µs |
| A search's compute on an 8 × 8 array | about 260 cycles for $\hat{Q}$ and 70 for the scan |
| A departure check's compute | about $B = 256$ cycles |

**A job is read-bound**: reading its block takes longer than computing on it, so jobs that overlap slow
each other at port A. That is the contention the study is about.

**The average load is small; bursts set the design.** At the proposed workload a channel's emitter
starts about 20 times a second, so 16 channels start about 300 searches a second, and the gate's false
alarms at $10^{-3}$ per block add about 600. At a few µs a job, that is well under 1% of one unit. What
loads the hardware is a **burst**: $k_{burst} = 8$ searches in one block, about 16 µs of reads queued at
one port, most of a 25.6 µs block period. The load sweep ($p_{on}$, $p_{burst}$, the gate's false-alarm
target) moves the design between idle and overloaded.
