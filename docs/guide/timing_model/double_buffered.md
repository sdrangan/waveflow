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

## Worked example: the matrix-LT FIR

> The [**Rowwise FIR** example](../../examples/rowwise_fir/) walks this end-to-end (model, hook, cosim,
> calibration); this section is the summary.

[`examples/rowwise_fir`](../../../examples/rowwise_fir/fir.py) (`FIRAccel`) is a shipped, cosim-
calibrated realization of this model — a per-matrix-row FIR. It keeps the three-process shape
(`load` / `compute` / `store` started in `pre_sim`, handing off through `transaction_queue`s) but
sharpens three things over the bare `simpy.Store` pattern above:

- **Per-direction channel resources, not one bus.** The ping-pong contention lives on the `m_axi`
  port as independent `read_channel` / `write_channel` resources (an AXI bundle is full-duplex, so a
  read and a write never contend). The component never wires bus contention by hand — the
  element-coordinate slice calls acquire the right channel automatically.
- **Element-coordinate pipelined transfers.** Load and store use `read_slice_pipelined` /
  `write_slice_pipelined` with `num_trans = n_row` (one burst per row) instead of `read_array` /
  `write_array`; the per-burst span comes from the port's calibrated bus timing.
- **The store hides under compute.** The Y-write is *early-anchored* and given `min_span =
  compute_body`, so it occupies the write channel for `max(write_occ, compute_body)` — finishing
  under compute's shadow when compute is the bottleneck. This is the double-buffered
  `max(load, compute, store)` made explicit on the store side (and it is the latency fix that lets
  the Y-write overlap the X-read).

The payoff of calibrating this model is that the whole-kernel latency decomposes into *physical*,
near-fit-free terms:

```
whole = (n_col + T) + trips + n_row·row_depth(n_col) + fill_const
        └ fill        └ II=1   └ per-row pipeline      └ one
          (first row)   compute   (the one fitted curve)  scalar
```

where `trips = n_row·(n_col − T + 1)`. The **channel occupancy** is deterministic (one transfer beat
per word), the **compute body** is exactly II=1, and the *only* fitted term is `row_depth(n_col)` —
the per-row ping-pong refill depth, a saturating lookup. How those terms are measured from a cosim
sweep and fit is [Fitting a timing model](./fit.md) and the [Calibration](../calib/) section; the
full end-to-end walkthrough is the `rowwise_fir` example.

## Where to log critical events

To validate a double-buffered model against RTL you compare *event timelines*, so the model emits a
timestamped event at each stage boundary. `FIRAccel` logs seven per command (`_log` in
[`fir.py`](../../../examples/rowwise_fir/fir.py)):

| event | logged at | bus-visible in RTL? |
|---|---|---|
| `cmd_arrive` | the loader pulls the command | no — the anchor (t = 0) for the comparison |
| `load_begin` / `load_end` | around the X-read | **yes** — the X-read burst span |
| `comp_begin` | compute starts (carries `compute_body`) | no — sim-internal |
| `store_begin` / `store_end` | around the Y-write | **yes** — the Y-write burst span |
| `resp_sent` | the response burst after Y | **yes** |

The rule of thumb: **log a begin/end pair around every transfer** (the bus-visible spans), plus the
command arrival as the anchor. The bus-visible events are exactly what the [VCD burst extractor](../timing/aximm.md)
recovers from cosim, so you anchor both timelines at `cmd_arrive` and compare each later event's
offset — the comparison that drives the calibration gates.

## Synthesis mapping

> **This page teaches an LT timing model, not a synthesizable structure.** The three internal
> SimPy processes are a *sim-only* device: their only job is to produce the
> `max(load, compute, store)` timeline. They use nothing new — three `self.process(...)`
> coroutines and two `simpy.Store`s (or `transaction_queue`s) — so the model works **today**, with
> no added framework capability.

There are two synthesizable realizations of this overlap, and the FIR example takes the first:

1. **A single hand-written `#pragma HLS DATAFLOW` hook (shipped).** One `@synthesizable` kernel whose
   body is the three sub-functions — load, compute, store — wired in a per-block DATAFLOW region over
   a partitioned-BRAM ping-pong and an `hls::stream` FIFO. This is exactly the
   [Dataflow custom-hook pattern](../custom_hooks/dataflow.md), and it is how `rowwise_fir` synthesizes
   today. It needs no new framework capability — the hook is the whole kernel.

2. **Hierarchical multi-`HwComponent` composition (deferred).** The same overlap expressed *in
   Waveflow* as a **parent component** owning three **dataflow sub-components** with shared top-level
   PIPO buffers and `hls::stream` handshakes — codegen lowering the sub-graph to the DATAFLOW region.
   That is gated on hierarchical composition (the accelerator-anatomy convergence work) and is **not
   in this docs pass**; until then, hand-write the hook (path 1).

## See also

- [Block processing](./block.md) — `capacity=1` collapses double-buffering back to this.
- [Streaming processing](./streaming.md) — finer-grained overlap (per element) when the source delivers a continuous stream rather than discrete blocks.
- [Dataflow custom hook](../custom_hooks/dataflow.md) — the synthesizable side: the three sub-functions in a `#pragma HLS DATAFLOW` region (the FIR realization).
- [Fitting a timing model](./fit.md) / [Calibration](../calib/) — how the matrix-LT FIR's terms (occupancy, compute, `row_depth`) are measured from cosim and fit.
- [Hardware Components](../components/) — declaring components and their ports; the home of the future hierarchical-composition milestone (synthesis path 2).
- [Process generators](../sim/procgen.md) — spawning concurrent SimPy processes, the mechanism this flow relies on.
- [`examples/rowwise_fir`](../../../examples/rowwise_fir/fir.py) — the worked, cosim-calibrated matrix-LT FIR.
