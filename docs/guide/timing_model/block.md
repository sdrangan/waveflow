---
title: Block processing
parent: Timing Models
nav_order: 4
audience: python
api: [SobIFSlave.acquire_read, StreamIFMaster.write, LinCalibModel, SimObj.timeout, Clock.period]
summary: "Inserting a timing model in a block process: the compute cannot start until the whole block has loaded, so the predicted compute delay goes AFTER the load and BEFORE the store. Whatever model the custom hook uses (typically the loop model), it is charged there. Shown on a stream-of-blocks Compute sub-component (acquire_read the block, compute, timeout, write). The load and store are already timed by the block/stream yields, so the model adds only the compute, and load/store stalls grow the firing on their own."
---

# Block processing

**Block processing** is the case where the compute needs the **whole block before it can start** — no
overlap with the load. It is the simplest place to insert a timing model, and the right starting point.

## Where the prediction goes: after the load, before the store

The rule is simple and general: **whatever model you choose for the custom hook, charge its predicted
delay after the load and before the store.** In a block process the compute reads a resident block,
runs, and writes a result — so the delay sits between the read and the write, exactly the shape of the
general pattern in [Adding a timing model](./insertion.md).

Here it is on a [stream-of-blocks](../concurrency/python/sob.md) `Compute` sub-component — the canonical
block process, since double-buffering is now built by composing `Load` / `Compute` / `Store` over SOB
channels:

```python
class Compute(FreeRunMod):
    def __post_init__(self):
        super().__post_init__()
        self.blk = SobIFSlave(name=f"{self.name}_blk", sim=self.sim, bitwidth=WORD_BW, block_n=NWORDS)
        self.z   = StreamIFMaster(name=f"{self.name}_z", sim=self.sim, bitwidth=WORD_BW)
        self.tm  = ...                     # a loop timing model — see loops.md
        self.add_timing_model(self.tm)
        for ep in (self.blk, self.z):
            self.add_endpoint(ep)

    def run_iter(self):
        blk = yield from self.blk.acquire_read()          # blocks until the WHOLE block is resident
        x   = read_array(blk, elem_type=Float32, word_bw=WORD_BW, shape=N)
        y   = compute(x)                                  # the value — computed instantly in pysim

        # the block is loaded; charge the compute time BEFORE writing the result
        cycles = self.tm.predict({"n": N})
        yield self.timeout(cycles * self.clk.period)

        yield from self.z.write(array(Float32, y))        # the store
        yield from self.blk.release_read()
```

`acquire_read` returns only after `Load` has committed a *full* block, so "the compute starts after the
load" is enforced by the channel, not by hand. The predicted `cycles` come from whatever model the hook
uses — for a loop, the `latency + ii·(m − 1)` model of [Timing models for loops](./loops.md). (A plain
component that `read_array`s a whole array, computes, and `write_array`s has the identical shape — read,
predict, write.)

## The delay is *additional*, not end-to-end

The model charges time only for the **compute** — not the load or store. That is deliberate, and it is
what keeps the model composable:

- **The load and store times are already accounted for.** `acquire_read` blocks until the block is
  resident (the SOB fill is timed), and `write` charges the output transfer — both by their `yield`s.
- So the modeled delay is the **compute, plus any overhead not already charged** by those transfers —
  the part of the firing that is genuinely *this component's* to model.
- And if the load or store **stalls** — a downstream FIFO is full, or the producer is behind — those
  `yield`s simply take longer, so the firing's end-to-end time **grows on its own**. The stall is the
  *simulation* modeling congestion, not something the timing model has to predict.

We deliberately do **not** add a fixed load/store time: it is not the component's to decide — it varies
with interconnect occupancy or a source outside the component. Letting the channels charge it keeps the
cost honest, and lets a model calibrated in isolation still predict the contended system.

> A timeline figure for this page (deferred) would show three non-overlapping bars — **load**, then
> **compute**, then **store** — making the serial barrier visually obvious.

## See also

- [Timing models for loops](./loops.md) — the `latency + ii·(m − 1)` model the `tm` above usually is.
- [Adding a timing model to a component](./insertion.md) — the general read → predict → write pattern
  this specializes.
- [Streaming processing](./streaming.md) — the overlapped case: compute *while* the data loads.
- [Stream of Blocks](../concurrency/python/sob.md) — the `acquire_read` / block channel used here, and
  how `Load` / `Compute` / `Store` compose into a double-buffered pipeline.
