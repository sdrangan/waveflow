---
title: SSR FFT timing results
parent: Waveflow's FFT, at full rate
nav_order: 2
summary: "What the RTL measured on the RFSoC 4x2 from L = 16 to 4096: a frame every L/R cycles at every length (7.5 to 10 times the vendor core), a first-frame latency of about 19L/16 cycles, and an isolated frame's latency identical to the back-to-back one -- no dependence on arrival. The pysim against the RTL, and what the SOB reorder costs."
---

# Timing

All numbers are RTL, XSI, the RFSoC 4x2 target (`xczu48dr`) at 250 MHz, read at the ports from the
BFMs, every frame bit-exact -- from `examples/ssr_fft/measured.json`, written by `ssr_fft_measure`.

## A frame every `L/R` cycles

![Cycles per frame against L, log-log: VitisFft 7.5 to 10 times above SsrFft, which sits on L/R](images/interval.svg)

| L | interval | first frame | isolated frame: first in → last out | isolated frame: last in → last out | `VitisFft` interval |
|---|---|---|---|---|---|
| 16 | 4 | 40 | 40 | 37 | 41 |
| 64 | 16 | 109 | 109 | 94 | 120 |
| 256 | 64 | 350 | 350 | 287 | 480 |
| 1024 | 256 | 1276 | 1276 | 1021 | 2556 |
| 4096 | 1024 | 4938 | 4938 | 3915 | 10240 |

(cycles; ping-pong reorder.  "first frame" is the first of 8 frames back to back; "isolated" is 12
frames each preceded by a random idle gap long enough to empty the pipeline.)

- **The interval is `L/R` exactly**, at every length -- the rate the architecture is built for, and
  7.5 to 10 times the vendor core's on the same platform. Nothing about it is fitted: every task moves
  one word a cycle and never stops between frames.
- **The latency is about `19L/16` cycles plus 3-4 per task**: the commutators' delays, one frame in
  the reorder buffer, and the frame's own transfer ([why](../../guide/dsp/ssr_fft/timing.md#the-latency-is-a-sum)).
- **An isolated frame takes exactly as long as the first frame of a burst** -- one value per length,
  across every gap. A commutator decides each group when its first word arrives, so a frame never
  waits for a free-running window to come round. The vendor core spreads the same "last in → last
  out" measure over a range at every length (105 to 123 at `L = 64`) and needs its mean and spread
  calibrated per length; this one needs neither.

At `L = 16` an isolated frame's processing (37 cycles from its last input word) is close to the
vendor core's (39): the difference there is almost entirely the interval.

## The pysim against the RTL

The composite pysim -- one process per task, timed cut-through from each frame's first word
([how](../../guide/dsp/ssr_fft/timing.md#the-pysim-one-process-per-task-cut-through)) -- lands within
3 cycles of the RTL's first frame at `L = 16 .. 1024` (40, 109, 349, 1273 against 40, 109, 350, 1276),
with the interval exact; `tests/dsp/ssr_fft/
test_hw.py` pins it. A four-frame pysim costs 0.1 s at `L = 64` and 1.6 s at `L = 1024`; the RTL
measurement of one length cost 1.5-2.3 minutes of csynth and 15-30 s of XSI.

## What the SOB reorder costs

With `reorder="sob"` -- a `stream_of_blocks` between a writer and a reader task -- `L = 64` measures
an interval of **21**, against 16 with the ping-pong buffer; the first frame takes 111 cycles against
109. The reader is re-entered, and its lock re-acquired, once a frame: five cycles a frame, 31% at
`L = 64`, 2% at `L = 1024`. (Four with `lanes=True`, where the reader writes an internal FIFO rather
than the output port.) Both are bit-exact, and both ran 500 frames back to back without a
stall.
