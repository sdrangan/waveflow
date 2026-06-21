---
title: Double-buffered processing
parent: Timing Models
nav_order: 4
audience: python
api: [MMIFMaster.read_array, MMIFMaster.write_array, SimObj.timeout, SimObj.process, Clock.period]
summary: "The double-buffered (ping-pong) timing model: load(n) ∥ process(n-1) ∥ store(n-2). Block-granularity overlap that cannot be expressed in one serial coroutine — it needs three concurrent SimPy processes handing off through depth-2 buffers, from which the steady-state max(load, compute, store) emerges."
---

# Double-buffered processing

**Double-buffered** (or **ping-pong**) processing sits between [block](./block.md) and
[streaming](./streaming.md) on the [overlap continuum](./index.md#three-flows-one-continuum). It
overlaps at **block** granularity: while block *n* is loading, block *n-1* is being computed and
block *n-2* is being stored.

```
load(n) ∥ process(n-1) ∥ store(n-2)
```

All three stages run at once on different blocks, so once the pipeline fills, the time per block
is set by the *slowest* stage rather than the sum of all three — roughly
`t_block ≈ max(load, process, store)`.

## Why this flow needs a different shape

You **cannot** model this overlap inside one sequential coroutine. In block and streaming, the
whole timing model fits in a single `run_proc` because the stages really are sequential there. But
in a double-buffered flow the stages must run *concurrently*, and the `yield from` on
`read_array` / `write_array` genuinely advances the clock and **serializes** — the moment you
`yield from` a load, that coroutine is blocked until the load finishes, so nothing else in it can
overlap. A single coroutine cannot be in a load and a store at the same simulated instant.

So this flow requires **actual concurrency**: three separate SimPy processes — a loader, a
compute stage, and a storer — handing blocks off to each other through bounded buffers.

## The pattern

```python
import math
import simpy

buf_in  = simpy.Store(env, capacity=2)   # ping-pong: capacity 2 == double buffer
buf_out = simpy.Store(env, capacity=2)

def loader():
    for blk in blocks:
        x = yield from mem.read_array(Float32, n, blk.xaddr, word_bw=word_bw)
        yield buf_in.put(x)              # blocks if the loader gets 2 blocks ahead

def compute_proc():
    for blk in blocks:
        x = yield buf_in.get()
        y = compute(x)
        m = math.ceil(n / self.unroll_factor)
        yield self.timeout((self.latency + self.proc_ii * (m - 1)) * self.clk.period)
        yield buf_out.put(y)

def storer():
    for blk in blocks:
        y = yield buf_out.get()
        yield from mem.write_array(y, Float32, blk.yaddr, word_bw=word_bw)
```

The three functions are started as concurrent processes (e.g. `self.process(loader())`,
`self.process(compute_proc())`, `self.process(storer())`) so SimPy interleaves them.

> A timeline figure for this page (deferred) would show three staggered rows — load, compute,
> store — each one block behind the row above, with the steady-state block period set by the
> tallest bar.

## Key points

- **The overlap is emergent, not computed.** You do not calculate `t_block ≈ max(load, process,
  store)` anywhere — it *falls out* of SimPy scheduling. Each stage simply does its work and blocks
  on the buffer hand-off; the discrete-event runtime resolves who waits for whom, and the
  steady-state throughput is whatever the slowest stage allows. This is the payoff of modeling with
  real processes instead of arithmetic.
- **`capacity=2` is what makes it "double" buffered.** The depth-2 `Store` bounds how far ahead a
  producer may run: the loader can be at most one block ahead of compute before `buf_in.put`
  blocks. That one block of look-ahead is exactly the second buffer.
- **The capacity is the knob across the whole continuum.** `capacity=1` removes the look-ahead and
  collapses this back to [block](./block.md) processing (load and compute can no longer overlap).
  `capacity=∞` is unbounded buffering (the loader races ahead with no back-pressure). `capacity=2`
  is the canonical ping-pong middle ground.

## Synthesis mapping (deferred)

> **This page teaches an LT timing model, not a synthesizable structure.** The three internal
> SimPy processes are a *sim-only* device: their only job is to produce the
> `max(load, compute, store)` timeline. They use nothing new — three `self.process(...)`
> coroutines and two `simpy.Store`s — so the model works **today**, with no added framework
> capability.

The model does have a synthesizable counterpart, worth naming so you know where it lands. In HLS
the same overlap is the canonical `#pragma HLS DATAFLOW` decomposition: a **parent `HwComponent`**
containing three **dataflow sub-components** — load, compute, store — with the AXI-MM interface
exposed to the load and store sub-components, **shared input/output PIPO buffers** declared at the
top level, and **`hls::stream` handshakes** between the stages signaling data-ready. That is the
hardware realization of the loader/compute/storer processes and the depth-2 buffers above.

Building it is gated on **hierarchical multi-`HwComponent` composition** — a parent component that
owns sub-components and wires shared memory and streams between them — which the current
single-component examples do not do yet. It therefore lands with that milestone (the
accelerator-anatomy convergence work on hierarchical composition), **not in this docs pass**. Until
then, this page ships the sim-only LT timing model and forward-references the synthesizable
structure.

## See also

- [Block processing](./block.md) — `capacity=1` collapses double-buffering back to this.
- [Streaming processing](./streaming.md) — finer-grained overlap (per element) when the source delivers a continuous stream rather than discrete blocks.
- [Hardware Components](../components/) — declaring components and their ports; the home of the future hierarchical-composition milestone this flow's synthesizable structure depends on.
- [Custom Hooks](../custom_hooks/) — the synthesizable side, where the `#pragma HLS DATAFLOW` body would be written.
- [Process generators](../sim/procgen.md) — spawning concurrent SimPy processes, the mechanism this flow relies on.
