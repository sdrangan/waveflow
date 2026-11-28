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
| Sample rate per channel | $f_s$ | 30.72 MHz | no |
| Noise figure | $F$ | 7 dB | no |
| Receiver height, downtilt | $h_{rx}$, $\delta$ | 25 m, 6° | no |
| Sector: ranges, half-width | $r_{min}$, $r_{max}$, $\phi_{max}$ | 100 m, 5 km, 60° | no |
| Emitter height, power | $h_e$, $P_t$ | 1.5 m, uniform 5 to 30 dBm | no |
| Statistical mode: per-element SNR | | uniform in $[-20, 30]$ dB | no |
| On and off probabilities per block | $p_{on}$, $p_{off}$ | 3/16, 3/16 | as a load sweep |
| Burst probability per block, size | $p_{burst}$, $k_{burst}$ | 1/50, 4 | as a load sweep |
| Input gain: the noise's complex std | $\sigma$ | 4 LSBs ([the input gain](workload.md#the-samples)) | yes |
| Seed, length | | per scenario | no |

## Algorithm

On [The algorithm](algo.md).

| Parameter | Symbol | Proposed | Explored | Scored by |
|---|---|---|---|---|
| Array | $n_h \times n_v$, $n_{rx}$ | 4 × 4, 16 | no | |
| Channels | $C$ | 8 | no | |
| Block length (samples) | $B$ | 256 | yes | A, R, T |
| Direction grid | $L_\phi \times L_\varepsilon$, $L$ | 32 × 8, 256 | yes | A, R, T |
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
| Energy sum | power detector | 44 bits ($B\, n_{rx} = 4096$ terms of 32 bits) | compared directly | -- | no | |
| Gram matrix $\hat{Q}$ | systolic unit | 41 bits | **18 bits**, after the $1/B$ shift | saturate | yes | A, R |
| Steering vectors $U$ | constant table | -- | 16 bits | -- | yes | A, R |
| Scan powers $p_\ell$ | systolic unit | 44 bits | **24 bits** | saturate | yes | A, R |
| Departure projection $u^H X$ | vector unit | 37 bits | **18 bits** | saturate | yes | A, R |
| Departure statistic $p_b$ | vector unit | 44 bits | **24 bits** | saturate | yes | A, R |
| Thresholds | registers | -- | as the value each compares against | -- | follows | |

Why these stored widths: 18 bits fits a DSP's narrow input (the xczu48dr's DSP48E2 multiplies 27 × 18 bits), so
a wider $\hat{Q}$, $U$ or $u^H X$ costs a second DSP per multiply; 24 bits keeps a power's dynamic
range over the workload's 45 dB of SNR. Where the strong tail of the workload ([up to 40 dB per
element](workload.md#the-geometry-emitters-on-the-ground)) saturates $\hat{Q}$, the scan's peak
flattens and the angular error grows. That is the trade the golden measures.

In the golden (`examples/aoa_detect/golden.py`) each stored value's shift is set from the noise
level by a **headroom** in bits: how far below full scale the noise-only value sits. The headroom is
the parameter; at the proposed $\hat{Q}$ headroom of 10 bits, $\hat{Q}$ saturates for emitters above
about 30 dB per element, inside the workload's tail.

**The input format reaches the timing.** A ring word read at port A holds 4 samples at 16 bits, 5 at
12 bits packed, 8 at 8 bits. Jobs are read-bound ([below](#what-they-imply)), so a narrower input
shortens every read and every decision, at a cost in accuracy and in the strong tail's clipping.

## Memory and queues

| Parameter | Symbol | Proposed | Explored | Scored by |
|---|---|---|---|---|
| Ring depth (slots) | $R$ | 8 | yes | R, T |
| Ring memory | | URAM, true dual port | no | |
| Ring port B (capture) width | | one sample word a cycle: 512 bits | follows | |
| Ring port A (readers) width | | 128 bits | yes | R, T |
| Crossbar data width, read burst | | 128 bits, 64 beats | yes | R, T |
| State-message queue depth | | $C$ (every channel answered at once) | no | |
| Result-message queue depth | | 64 | no | |
| Summary period | $P$ | 1,000 blocks (8.3 ms) | no | |

## Credits

| Parameter | Proposed | Explored | Scored by |
|---|---|---|---|
| Notification credits (detector → controller) | 16 | yes | T |
| Dispatch credits per unit (controller → a unit) | 2 | yes | T |

## Units

| Parameter | Symbol | Proposed | Explored | Scored by |
|---|---|---|---|---|
| Systolic units | $N_s$ | 1, 2, 4 | yes | R, T |
| Systolic array | $R_s \times C_s$ | 8 × 8; 16 × 16 (one Gram output per cell at $n_{rx} = 16$) | yes | R, T |
| Vector units | $N_v$ | 1, 2 | yes | R, T |
| Vector lanes | $V$ | 16 ($= n_{rx}$) | no | |

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
| Block period, $B / f_s$ | 8.33 µs, 2,083 cycles at 250 MHz |
| Capture rate | $C\, n_{rx}\, f_s = 3.9$ G samples/s: 16 samples, one 512-bit word, a cycle |
| One channel's block in the ring | $B\, n_{rx} = 4096$ samples, 16 KB |
| One slot (all channels) and the ring | 128 KB; $R = 8$ is 1 MB (URAM) |
| A job's read at port A, alone | 4096 samples at 4 a beat: 1,024 cycles, half a block |
| A search's compute on an 8 × 8 array | $\hat{Q}$ in 4 tiles of 256 cycles, the scan in 64 tiles of 16: about 2,000 cycles, one block |
| A search's compute on a 16 × 16 array | about 256 + 256 cycles: the read (1,024) binds |
| A departure check's compute | about $B = 256$ cycles; its read (1,024) binds |

**The array size decides what binds.** On an 8 × 8 array a search is compute-bound, about one block
long; on a 16 × 16 it is read-bound, and jobs that overlap slow each other at port A.

**The load: about 1.5 activations a block.** A channel changes state $2\, p_{on} p_{off} / (p_{on} +
p_{off}) = 0.19$ times a block (an arrival and a departure per emitter), so 8 channels give about 1.5
jobs a block: about 0.75 searches and 0.75 departure checks, plus the gate's false alarms ($10^{-3}$
per idle channel-block, negligible). With a search lasting about a block on an 8 × 8 array, one
systolic unit is about 75% busy on average; the bursts ($k_{burst} = 4$ channels at once, every 50
blocks) push it past 100% for a while. Emitters last $1/p_{off} \approx 5$ blocks, 44 µs, so a decision
a block late is a large part of an emitter's life. The load sweep ($p_{on}$, $p_{off}$, $p_{burst}$)
moves the design between idle and overloaded.

**The golden at these values**, on the statistical workload (3,000 blocks, seed 31, 2,313 emitters;
SNR in dB per element, at 50% and 90% detection):

| Variant | SNR at 50% | SNR at 90% | Missed | Of them, format loss | Early stops | Angular error, median | Searches |
|---|---|---|---|---|---|---|---|
| float | −14.8 | −12.8 | 244 | -- | 59 | 1.2° | 2,114 |
| proposed formats | −14.8 | −12.8 | 243 | 0 | 59 | 1.2° | 2,113 |
| $\hat{Q}$ in 12 bits | −4.4 | −4.0 | 748 | 504 | 0 | 1.2° | 3,962 |
| 8-bit input | −14.8 | −12.8 | 243 | 0 | 59 | 1.2° | 2,113 |
| grid 16 × 4 | −14.8 | −12.8 | 243 | 0 | 62 | 2.2° | 2,114 |
| gate at $10^{-2}$ | −16.3 | −13.1 | 180 | 2 | 113 | 1.2° | 2,354 |

What it shows: the proposed formats lose nothing against float; a 12-bit $\hat{Q}$ moves the detection
curve up by about 9 dB and, by failing to detect, nearly doubles the searches; a coarser grid doubles
the angular error but costs nothing in detection; and a gate ten times more sensitive gains 1.5 dB at
50% detection for 11% more searches and twice the early stops (weak emitters detected, then lost). That
last row is the accuracy–load coupling the timed phase has to resolve. No false detection in any run:
the detection threshold is set for $10^{-4}$ per search.
