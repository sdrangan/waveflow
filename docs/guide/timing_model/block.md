---
title: Block processing
parent: Timing Models
nav_order: 2
audience: python
api: [MMIFMaster.read_array, MMIFMaster.write_array, SimObj.timeout, Clock.period]
summary: "The block-processing timing model: load a whole array, compute, store. A single serial coroutine — no overlap — so it is the simplest to model. The compute wait is the closed form latency + II·(m − 1), with the (m − 1) convention spelled out."
---

# Block processing

**Block processing** is the serial barrier: load the whole input array into a buffer, compute on
it, then store the whole output array. Nothing overlaps — load, compute, and store happen strictly
one after another. It is the simplest flow to model, and the right starting point.

A block-processing component's `run_proc` looks like this (the timing fields `self.latency`,
`self.proc_ii`, and `self.unroll_factor` are the component's own synthesis parameters — see
[Fitting a timing model](./fit.md)):

```python
import math

x = yield from mem.read_array(Float32, n, xaddr, word_bw=word_bw)
y = compute(x)

# Wait the time to process `x`
m = math.ceil(n / self.unroll_factor)        # effective trip count
cycles = self.latency + self.proc_ii * (m - 1)
yield self.timeout(cycles * self.clk.period)

yield from mem.write_array(y, Float32, yaddr, word_bw=word_bw)
```

> A timeline figure for this page (deferred) would show three non-overlapping bars in sequence —
> **load**, then **compute**, then **store** — making the serial barrier visually obvious.

## Why there is no explicit load/store time

Notice the model charges time *only* for compute. The load and store time is already handled by
the `yield from` on [`read_array`](../interface/aximm.md) / `write_array`: those calls block the
coroutine for however long the transfer actually takes. We deliberately do **not** add a fixed
time for loading, because the load time is not the processor's to decide — it varies with
interconnect occupancy (other masters contending for the bus, see [polling
overhead](../interface/poll.md)) or with a source outside the processor entirely. Letting the
interface charge it keeps the load/store cost honest and composable; the component models only the
part it owns, the compute.

## `ii` and `latency`

Two numbers describe a pipelined compute:

- **`ii`** — the **initiation interval**: the number of cycles between successive iterations
  entering the pipeline. An `II = 1` pipeline accepts a new iteration every cycle; `II = 2` every
  other cycle. It sets the *throughput*.
- **`latency`** — the **pipeline depth**: the number of cycles from an input entering the pipeline
  to its corresponding output coming out. It sets the *delay* of any single result.

`unroll_factor` is the third knob: unrolling the loop by *U* processes *U* elements per iteration,
so the **effective trip count** — the number of iterations actually issued — is
`m = ceil(n / U)`, not `n`.

## The closed form `latency + II·(m − 1)`

For a pipeline that issues `m` iterations, the total compute time is:

```
cycles = latency + II · (m − 1)
```

The `(m − 1)` is deliberate, and worth stating explicitly. Reason it out from the two endpoints:

- The **first** output lands `latency` cycles after compute starts (it has to traverse the whole
  pipeline).
- After that first output, the pipeline emits **one more output every `II` cycles**. There are
  `m − 1` *remaining* outputs after the first, so they take `II · (m − 1)` additional cycles.

Add them: `latency + II · (m − 1)`. A common off-by-one is to write `latency + II · m`, which
over-counts by one `II` — it charges an initiation interval for the first output, which has already
been paid for inside `latency`. With `m = 1` (a single iteration), the formula correctly collapses
to just `latency`.

## Fitting the numbers

`latency`, `ii`, and `unroll_factor` are properties of the synthesized hardware. You can set them
by hand from an HLS report, or **fit** them from a few measured data points so the LT model tracks
real cycle counts — that is covered in [Fitting a timing model](./fit.md).

## See also

- [Streaming processing](./streaming.md) — the next step on the continuum: let compute overlap the load instead of waiting for the whole array.
- [MM Interfaces](../interface/aximm.md) — the `read_array` / `write_array` calls and their transfer-latency model.
- [Simulation timing model](../sim/timing.md) — `self.timeout` and the `Clock` that converts cycles to seconds.
