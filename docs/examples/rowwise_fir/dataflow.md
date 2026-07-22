---
title: The load-compute-store dataflow
parent: Rowwise FIR
nav_order: 2
has_children: false
audience: python
summary: "Why the rowwise FIR is a load-compute-store dataflow — the sliding window can't stream, so each row is loaded resident, computed over, and stored, with the three stages pipelined across rows (load(n+1) ∥ compute(n) ∥ store(n-1)). The two channel kinds: a partitioned-BRAM ping-pong for windowed input, a FIFO for sequential output."
---

# The load-compute-store dataflow

[FIR forces a resident row buffer](./fir.md#why-fir-forces-a-resident-buffer). The
shape that follows is **load-compute-store**: read a whole row in, compute the windowed sum over the
resident buffer, write the output row out — and *overlap* the three across successive rows.

```
row n-1:                                store
row n:                      compute
row n+1:        load
                ───────────────────────────────▶ time
```

Once the pipeline fills, the three stages run at once on different rows, so the time per row is set by
the **slowest** stage, not the sum — `t_row ≈ max(load, compute, store)`. This is the
[double-buffered timing model](../../guide/concurrency/python/sob.md); rowwise FIR is its worked,
shipped example.

## Two channels, by access pattern

The overlap needs the stages to hand off data without blocking each other, and the *kind* of channel
follows the *access pattern*:

- **load → compute: a partitioned-BRAM ping-pong.** Compute reads the row through a sliding window —
  `xbuf[j + (T−1) − t]` — which is *random* access, so the channel must be an on-chip buffer, not a
  FIFO. Two buffers (ping-pong) let load fill row `n+1` while compute reads row `n`.
- **compute → store: an `hls::stream` FIFO.** The outputs are produced and consumed strictly in order,
  so a sequential FIFO is the right (and cheapest) channel.

## Why not the alternatives

- **Not a pure stream.** The window needs random back-reference into the row, which a beat-at-a-time
  stream can't provide (that is the whole reason for the resident buffer).
- **Not a serial block.** Loading the entire row, *then* computing, *then* storing — with no overlap —
  is the simplest model but pays the full `load + compute + store` latency per row. The double buffer
  hides two of the three behind the slowest.

## The two sides of this example

Load-compute-store has a **simulation** side and a **synthesis** side, and rowwise FIR shows both:

- the **Python model** — three concurrent SimPy stage processes producing the overlapped timeline
  ([The Python model](./pymodel.md));
- the **Vitis kernel** — three HLS functions in a `#pragma HLS DATAFLOW` region over the same
  ping-pong + FIFO ([Writing the dataflow hook](./kernel_hook.md)).

They are the two faces of one accelerator, checked against [one golden](./fir.md#the-golden).
