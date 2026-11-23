---
title: Timing a composite
parent: Timing Models
nav_order: 9.7
audience: python
api: [copy_burst, StreamIFSlave.begin_read, StreamIFSlave.end_read, StreamIFMaster.begin, wait_any]
summary: "What a composite's pysim needs beyond calibrated blocks, learned on os_filterbank (a loop of eight tasks around one shared FFT, 27% short of its RTL while every gate passed, now within 2.7%): every task in the loop timed, not only the library's; each block calibrated on the input patterns it sees in the composite, gapped frames included; cut-through at burst granularity, not store-and-forward and not word by word; and a cycle gate against the RTL, both directions, a stated tolerance. A checklist, the reason for each item, and what it cost in speed (1-4% more wall time)."
---

# Timing a composite

Calibrated blocks are necessary, not sufficient. A composite -- a pipeline, a shared kernel's loop, a pool of
workers -- is timed right in pysim only when four more things hold. Each item below is a cause found in
[`examples/os_filterbank`](../../examples/os_filterbank/index.md), whose pysim ran 9-27% short of its RTL
while every RTL gate passed (they compared words, not cycles), and which now lands within 2.7% (`test`) and
0.6% (`long`) of it; the record is [Timing fidelity: os_filterbank](../../evaluation/timing_fidelity/os_filterbank.md).

## The checklist

1. **Every task in the loop is timed, not only the library's.**
2. **Each block is calibrated on the input patterns it sees in the composite**, gapped frames included.
3. **A block that copies through is cut-through at burst granularity** -- it commits on a frame's first
   word and copies as the words arrive -- not store-and-forward, and not word by word.
4. **The composite has a cycle gate against its RTL**: pysim's cycles against the cycles its RTL gate pins,
   in both directions, a stated tolerance.

### 1. Every task timed

A library block (`SsrFft`, `StreamArbiter`, `StreamDemux`) comes with its timing. A design's own stage often
does not: its `run_iter` moves its bursts and charges only their links' word rate. In os_filterbank the
three example stages -- `front`, `pre`, `post` -- spent cycles their models did not: a loop's entry and exit,
a header's clear loop, a block read a word a cycle after its credit. At the start they were 48 cycles of
`test`'s gap (`post` 21, `front` 15, the first block's start 12), and `pre`'s copy-loop entry 9 more, found
last; each sits on the loop, so every frame after it is late too.

Charge what the stage's HLS body spends, from its loop structure, read off a traced RTL run at each loop's
least (the stage idle, nothing queued). os_filterbank keeps them in one table, `STAGE_CYC`, each a short
`timeout` in its `run_iter`, and reads a buffered burst a word a cycle with `end_read(charge_beats=True)`.
Never a constant at the end to absorb cycles no cause explains. When the stage is a candidate for reuse,
declare it as a [timed phase](./timed_phases.md) and calibrate it instead.

### 2. Calibrated on the patterns it will see

A block's model is only as good as the inputs it was measured on. `SsrFft` was measured back to back and on
isolated frames, and on both its interval is `L/R`. In the loop its frames come a cycle or more apart, and
there the RTL's interval is `max(2L/R, L/R + g)`: any gap costs a whole frame-time
([its timing](../dsp/ssr_fft/timing.md)). That one rule was 92-103% of the bank's gap. The same lesson at
smaller scale: the arbiter's decision table was measured at 32 bits and 1-burst frames, and at the bank's
128 bits and 2-burst frames every gap is a cycle longer.

So, for each block in the composite:

- **Measure the patterns the composite produces**: gapped frames, random gaps, the composite's width and
  frame size, its port count. A fixture that sweeps only back-to-back input misses exactly the case a loop
  makes.
- **Prefer a model that refuses what it has not seen.** The stream blocks' lookup tables
  (`waveflow/streams/timing.py`) refuse an unmeasured width, frame or port count when the block is built;
  a fitted line would have extrapolated silently.
- **Name the limits you leave.** `SsrFft`'s gap is counted from when a frame arrives, which is exact for a
  source whose frames come when their data does. A source the FFT stalls -- a testbench driver -- is 6-8%
  early in pysim on random gaps; the block's page and its tests say so.
- **Inside a composite a block can cost less than alone**: a firing on an internal FIFO, 2-4 cycles less
  for the dispatcher's credit. Where it matters, calibrate in context, by tracing the composite itself.

### 3. Cut-through, at burst granularity

An HLS block that forwards a frame commits to it on its first word and copies the rest as it arrives. A
pysim block that waits for the whole burst before it decides (store-and-forward) delays everything
downstream by a frame, and on a loop that delay comes round again. In os_filterbank that was `pre` (cause B),
`post`'s inverse pass, and the demuxes; in the pool of workers it sent first-free's jobs to the wrong worker.

The primitives are the begun burst: `begin_read(pending_ok=True)` returns a burst whose first word has
arrived, `StreamIFMaster.begin(...)` starts the output, `copy_burst(src, dst)` copies a burst as its words
arrive, and `wait_any(..., begun=True)` wakes on a first word. `StreamArbiter`, `StreamDispatch` and
`StreamDemux` use them (`docs/guide/streams/`).

**Bursts, not words.** pysim's links still move a burst at a time: the extra events are one per begun burst,
not one per word. Word-by-word back-pressure in the links was considered and rejected (2026-11-22): it would
make every word an event, and pysim's speed is the point. The cost of the limit is named (item 2's stalled
source) rather than modelled.

### 4. A cycle gate

A gate per composite: pysim's cycles at the points its RTL gate already pins -- os_filterbank's last status
on each channel -- compared both ways, within a stated tolerance, as a fast test with no vendor tools. Without
one, a composite's pysim drifts with every change to a block and nobody sees it: os_filterbank's moved from
9% to 17% short (`test`) and from 20% to 27% short (`long`) in one change that made two blocks *more*
accurate -- their store-and-forward had been hiding a larger error.

Choose the tolerance from the measured result and the size of what is left unexplained, per scenario where
the runs differ: os_filterbank's is 3% on `test` and 1% on `long`
(`test_pysim_timing_is_within_tolerance_of_the_rtl`). When it fails, the method on
[Timing fidelity](../../evaluation/timing_fidelity/index.md) finds the cause: a per-frame timeline in both
backends, the gap attributed hop by hop along the RTL's critical path.

## What it cost

Measured on os_filterbank, the same script on the same machine (`examples/os_filterbank/speed.py`): from
before this work to after it, the bank's pysim takes **1-4% more wall time** and processes 11-12% more
events, while the `long` scenario went from 20% short of its RTL to exact. Every fix here is a cycle count
or a begun burst; none adds an event per word.

## See also

- [Sharing a kernel](../systems/shared_kernels.md) -- the loops these rules were learned on.
- [Pipelined bodies](./streaming.md) and [Sequential bodies](./block.md) -- declaring a stage's own timing.
- [Timing fidelity](../../evaluation/timing_fidelity/index.md) -- the method, and the ledger of every
  discrepancy found.
