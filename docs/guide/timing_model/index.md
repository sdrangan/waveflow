---
title: Timing Models
parent: Guide
nav_order: 11
has_children: true
audience: python
api: [SimObj.timeout, Clock.period, MMIFMaster.read_array, MMIFMaster.write_array, StreamIFSlave.get_pipelined, StreamIFMaster.write_pipelined]
summary: "How an HwComponent specifies its timing model — not just what it computes, but how long that takes and what event it pends on to complete. Teaches three processing flows as a continuum of load/compute/store overlap: block, double-buffered, and streaming."
---

# Timing Models

Every `HwComponent` carries two models. The **functional model** says *what* it computes — the
math from inputs to outputs. The **timing model** says *how long* that takes and *what event it
pends on* to complete — when, in simulated time, the work finishes and the output becomes
available. The functional model is documented with each component; this section is about the
timing model.

> The name **timing model** may not be final — it is the *forward* model that produces a
> timeline (as opposed to the [Timing Analysis Tools](../timing/) that *measure* one). It builds
> directly on the [Simulation timing model](../sim/timing.md) page (`Clock`, `self.timeout`,
> charging compute latency) and goes deeper: how the **shape** of a component's load/compute/store
> loop changes the timeline.

## Three flows, one continuum

Many of the accelerators in Waveflow follow a three-step process: read input data, perform some computation on it, and write the output data.  This **load / compute / store** sequence arises often in accelerators, and designers typically use one of three models to realize it.  All three models have the same *functional* behavior but differ in their *timing* behavior — specifically, in the extent to which the steps overlap.  That overlap in turn affects which computations can be supported, the resource usage, and the latency.

 - **block**: load, compute, and store run sequentially with no overlap — a serial barrier: load the whole array, then compute, then store.  Simplest to implement, but has the highest latency.

 - **double-buffered**: overlap at *block* granularity (load(n) ∥ process(n-1) ∥ store(n-2)).  Reduces latency, but requires additional memory for buffering.

 - **streaming**: the limit as block size → 1 beat — per-element overlap.  Needs minimal buffering and achieves the lowest latency, but is only possible when the computation can be performed as data arrives.


The pages below take them in order of modeling difficulty — block first (a single serial
coroutine), then streaming (per-element timestamps in one coroutine), then double-buffered (which
needs *actual* concurrency).

## A note on naming

This section prefers **double-buffered** (or "ping-pong") over a bare "buffered." Block processing
also uses a buffer — it loads a whole array into one before computing — so "buffered" alone is
ambiguous. "Double-buffered" names the thing that matters: *two* buffers, so the next block can
load while the current one is still being processed.

## In this section

- [LT vs CT models](./models.md) — loosely-timed vs cycle-timed simulation, and why Waveflow is LT.
- [Block processing](./block.md) — the serial barrier: load → compute → store. The simplest to model, and the closed-form compute latency `latency + II·(m − 1)`.
- [Streaming processing](./streaming.md) — per-element overlap: the first output appears `latency` cycles after the *first* input, and the rest are gated by whichever is slower, input arrival or compute rate.
- [Double-buffered processing](./double_buffered.md) — block-granularity overlap (`load(n) ∥ process(n-1) ∥ store(n-2)`), modeled with three concurrent SimPy processes through depth-2 buffers.
- [Fitting a timing model](./fit.md) — recovering `latency`, `ii`, and `unroll_factor` from measured data points.

## See also

- [Simulation timing model](../sim/timing.md) — the `Clock`, `self.timeout`, and where transfer vs. compute latency is charged. This section assumes that page.
- [Timing Analysis Tools](../timing/) — the *reverse* direction: measuring throughput / latency / overlap from a produced timeline (VCD, cosim, AXI parsing).
- [Interfaces](../interface/) — `read_array` / `write_array` (memory-mapped) and `get_pipelined` / `write_pipelined` (streams), the transfer calls these flows are built from.
