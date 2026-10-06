---
title: SSR FFT timing
parent: The SSR FFT
grand_parent: DSP Blocks
nav_order: 5
audience: python
api: [SsrFft, latency_cycles, get_pipelined, write_pipelined, call_after]
summary: "Why SsrFft needs no calibrated timing table: its interval is L/R by construction, its latency is the sum of its tasks' -- (R-1)D per commutator, a frame for the reorder buffer, a few cycles of pipeline per task -- and an isolated frame's latency does not depend on when it arrives. How the composite pysim reproduces that with one process per task, timed cut-through from each frame's first word; and how close it lands: within 5 cycles of the RTL's first frame from L = 16 to 1024, the interval exact."
---

# Timing

A bit-exact model says what comes out. When it comes out takes two numbers -- the **interval**
between frames and the **latency** of one -- and for `SsrFft` neither needs a calibration table.

## The interval is `L/R`, by construction

Every task moves one word a cycle and never stops between frames. A frame is `L/R` words, so frames
follow each other every `L/R` cycles through every task, and through the chain. Measured in RTL, back
to back, every length: 4, 16, 64, 256, 1024 cycles at `L` = 16 ... 4096. There is nothing to fit.

## The latency is a sum

A frame's latency is what its tasks add up to:

| task | latency it adds |
|---|---|
| commutator, block size `D` | `(R − 1)·D` -- its definition -- plus its pipeline |
| stage, lane adaptor | its pipeline depth: a few cycles |
| reorder buffer | a frame (`L/R`): a frame must be written before its first word can be read |

Summed over the chain -- the transposer's commutators add `L/4 − 1` between them (their `D` are
`1, 4, ..., L/16`), the stages' commutators the same, the reorder's commutator `3L/16`, the buffer
`L/4` -- and with the frame's own transfer out, the first output frame completes about `19L/16` cycles after its first input word, plus
3-4 cycles per task. Measured:

| L | tasks | first frame done (cycles from its first input word) | `19L/16 − 3` |
|---|---|---|---|
| 16 | 7 | 40 | 16 |
| 64 | 10 | 109 | 73 |
| 256 | 13 | 350 | 301 |
| 1024 | 16 | 1276 | 1213 |
| 4096 | 19 | 4938 | 4861 |

**It does not depend on arrival phase.** A commutator decides each group when its first word arrives,
so an isolated frame finds every task in step. Measured at `L = 64`: 24 frames with random idle gaps
of 150 to 400 cycles all took exactly 109 cycles from first word in to last word out -- the same as
the first frame of a back-to-back run -- and exactly 94 from last word in to last word out; every
length measured shows the same (`examples/ssr_fft/measured.json`).
`VitisFft`'s vendor core, whose commutators run a free-running window, spreads that second measure
over 105 to 123 and needs a mean with a stated error ([its timing](../../vitis_l1/fft/timing.md)).

## The pysim: one process per task, cut-through

`SsrFft`'s pysim is its children's, one SimPy process per task, each handling a frame per firing:

```python
words, t_first = yield from self._get_words(self.s_in, n)   # the frame, and when its first word arrived
re, im = m.stage(self.geo, self.task.s, *self._unpack(words, e_in))   # the bits: the model's
start = t_first + self.lat * self.clk.period                # cut-through: from the FIRST word
self.call_after(0, self._store, self.m_out, out_words, start, self._wlock, slot=self._slots)
```

Two choices make it track the hardware:

- **Cut-through, not store-and-forward.** The output starts `lat` cycles after the frame's *first*
  word (`write_pipelined` with a start time), not after its last. The hardware streams; a pysim that
  waited for whole frames would charge every one of the 16 tasks a frame transfer, putting 16 frames
  of latency on a chain that has about one.
- **The write is deferred** (`call_after`), so a child reads its next frame while the previous one
  drains -- the interval comes out `L/R` by itself.

`lat` per task is `latency_cycles`: `3D + 3` for a commutator, 3 for a stage, a frame plus one for the
ping-pong reorder. The fixed terms were first csynth's pipeline depths, which put the first frame
about 1.9 cycles per task late (the pysim's channel hop already charges part of it); they were trimmed
once by 2. Against the RTL:

| L | pysim first frame | RTL first frame | interval, both |
|---|---|---|---|
| 16 | 40 | 40 | 4 |
| 64 | 109 | 109 | 16 |
| 256 | 349 | 350 | 64 |
| 1024 | 1273 | 1276 | 256 |

`tests/dsp/ssr_fft/test_hw.py` pins it: within 5 cycles at `L` = 16 ... 1024, the interval exact.

**Cost.** One process per task per frame, so a frame costs about as many SimPy events as there are
tasks, plus the vectorized arithmetic: 0.1 s for four frames at `L = 64`, 1.6 s at `L = 1024`. The
XSI run of the same four frames costs a 1.5-2 minute csynth and 10-20 s of simulation.

## What is open

- **A fused pysim.** A single process for the whole FFT, `VitisFft`-style -- same bits, the latency as
  one number -- for system simulations that do not need to see inside.
- **The SOB reorder in the pysim** is timed with a fixed reader start-up matched to XSI (5 cycles),
  which reproduces the RTL's `L/R + 5`; it is a constant fitted at one length (`L = 64`).
- **Latencies at `L = 4096`** and for widths other than 16 bits are unmeasured against the pysim.
