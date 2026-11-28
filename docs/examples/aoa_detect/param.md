---
title: The parameters
parent: Emitter detection and AoA
nav_order: 5
summary: "Every parameter of the design in one place, with a proposed value and whether it is fixed or explored: the platform, the workload, the algorithm (array, channels, block, direction grid, thresholds as false-alarm targets), the number formats from the input samples through the energy sums, the Gram matrix, the scan and the departure statistic, the ring and the queues, the credits, the units (how many, and the systolic array's shape), and the policies. Then the quantities they imply: the block period, the ring's size, a job's read and compute time, and the load, which is small on average and set by bursts and the gate's false alarms."
---

# The parameters

**Status: under construction.** Every value is a **proposal**. The formats in particular are first
guesses, to be fixed by a bit-exact model against floating point, as the earlier examples did. The
**explored** column marks what the design-space exploration varies; the rest is fixed for a study.

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
| Input scale | noise below full scale | to be set (see [the samples](workload.md#the-samples)) | yes |
| Seed, length | | per scenario | no |

## Algorithm

On [The algorithm](algo.md).

| Parameter | Symbol | Proposed | Explored |
|---|---|---|---|
| Array | $n_h \times n_v$, $n_{rx}$ | 4 × 2, 8 | no |
| Channels | $C$ | 16 | no |
| Block length (samples) | $B$ | 256 | yes |
| Direction grid | $L_\phi \times L_\varepsilon$, $L$ | 16 × 4, 64 | yes |
| Search gate (a.1) | $\tau_e$ | set from a false-alarm probability per block, $10^{-3}$ | yes |
| Detection (a.4) | $\tau_p$ | set from a false-alarm probability per search, $10^{-4}$ | yes |
| Tracking energy (b.1) | $\tau_e'$ | set so a present emitter rarely triggers b.2 | yes |
| Stop (b.2) | $\tau_\gamma$ | set from an early-stop probability per check, $10^{-4}$ | yes |

The thresholds are stored as numbers (pre-scaled to what each comparison sees), computed by the host
from these targets with the chi-square statistics on [the algorithm](algo.md).

## Number formats

All complex values have the given width for each of the real and imaginary parts. The accumulators
are sized not to overflow; what is stored after them is narrower.

| Value | Where | Proposed | Explored |
|---|---|---|---|
| Input sample | the stream, the ring | 16 bits (Q1.15) | yes |
| Sample word | the stream, the ring | one channel's $n_{rx}$ samples: $8 \times 32 = 256$ bits | follows |
| $\lvert x \rvert^2$ and the energy sum | power detector | 32-bit terms; the sum over $B\, n_{rx} = 2048$ of them in 44 bits (exact) | no |
| Gram products and sums | systolic unit | 33-bit products; sums over $B$ in 41 bits (exact) | no |
| $\hat{Q}$ | inside the systolic unit | 24 bits, after the $1/B$ shift | yes |
| Steering vectors $U$ | constant table in each unit | 16 bits | yes |
| Scan $W = \hat{Q} U$, and $p_\ell$ | systolic unit | 43-bit sums; $p_\ell$ stored in 32 bits | yes |
| $u^H X$ | vector unit | 36-bit sums, rounded to 24 bits | yes |
| $p_b$ | vector unit | squares summed over $B$, stored in 32 bits | yes |
| Thresholds | registers | as the value each compares against | follows |

## Memory and queues

| Parameter | Symbol | Proposed | Explored |
|---|---|---|---|
| Ring depth (slots) | $R$ | 8 | yes |
| Ring memory | | URAM, true dual port | no |
| Ring port B (capture) width | | 256 bits, one sample word a cycle | no |
| Ring port A (readers) width | | 128 bits | yes |
| Crossbar data width, read burst | | 128 bits, 64 beats | yes |
| State-message queue depth | | $C$ (every channel can be answered at once) | no |
| Result-message queue depth | | 64 | no |
| Summary period | $P$ | 1,000 blocks (25.6 ms) | no |

## Credits

| Parameter | Proposed | Explored |
|---|---|---|
| Notification credits (detector → controller) | 16 | yes |
| Dispatch credits per unit (controller → a unit) | 2 | yes |
| Jobs the controller holds when every unit is busy | the notification credits bound it | follows |

## Units

| Parameter | Symbol | Proposed | Explored |
|---|---|---|---|
| Systolic units | $N_s$ | 1, 2, 4 | yes |
| Systolic array | $R_s \times C_s$ | 8 × 8 (one Gram output per cell at $n_{rx} = 8$) | yes: 4 × 4, 8 × 8 |
| Vector units | $N_v$ | 1, 2 | yes |
| Vector lanes | $V$ | 8 ($= n_{rx}$) | no |

## Policies

| Policy | Options | Proposed |
|---|---|---|
| Notification order when credits are short | fixed channel order, departures first, round-robin, highest energy first | departures first, then round-robin |
| Controller's dispatch when both pools have work | independent pools | independent pools |
| A late read | no decision; the channel re-notifies on its next block | fixed |

## What they imply

At the proposed values:

| Quantity | Value |
|---|---|
| Block period, $B / f_s$ | 25.6 µs, 6,400 cycles at 250 MHz |
| One channel's block in the ring | $B\, n_{rx} = 2048$ samples, 8 KB |
| One slot (all channels) and the ring | 128 KB; $R = 8$ is 1 MB (URAM) |
| A job's read at port A, alone | 2048 samples at 4 a beat: 512 cycles, about 2 µs |
| A search's compute on an 8 × 8 array | about 260 cycles for $\hat{Q}$ and 70 for the scan, under 2 µs |
| A departure check's compute | about $B = 256$ cycles |

**A job is read-bound**: reading its block takes longer than computing on it, so jobs that overlap slow
each other at port A. That is the contention the study is about.

**The average load is small; bursts set the design.** At the proposed workload a channel's emitter
starts about 20 times a second, so 16 channels start about 300 searches a second. With the gate's
false alarms at $10^{-3}$ per block, about 600 more. At a few µs a job, that is well under 1% of one
unit. What loads the hardware is a **burst**: $k_{burst} = 8$ searches in one block, about 16 µs of reads
queued at one port, most of a 25.6 µs block period. The DSE sizes the units, the port and the credits
for the burst at a target decision time, and the load sweep ($p_{on}$, $p_{burst}$, the gate's
false-alarm target) moves the design between idle and overloaded.
