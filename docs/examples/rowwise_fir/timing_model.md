---
title: The timing model
parent: Rowwise FIR
nav_order: 4
has_children: false
audience: python
api: [InterpCalibModel]
summary: "FIRTiming: which parts of the accelerator are timed and how. The whole-kernel decomposes into deterministic channel occupancy (transfer beats), an exact II=1 compute body, and a single calibrated curve row_depth(n_col). The store is early-anchored so it hides under compute; the sim composes whole = fill + max(write_occ, compute_body). Plus the per-stage log events used to validate against cosim."
---

# The timing model

The functional model says *what* `FIRAccel` computes; the **timing model** says *how long* and *what it
pends on*. For rowwise FIR it is `FIRTiming` ([`fir.py`](../../../examples/rowwise_fir/fir.py)), and its
defining property is that it is **physical and near-fit-free** — almost everything is a known constant
or an exact rate, with a *single* calibrated curve.

## The decomposition

The whole-kernel latency for one command composes as:

```
whole = (n_col + T) + trips + n_row·row_depth(n_col) + fill_const
        └ fill        └ II=1   └ per-row pipeline      └ one
          (first row)   compute   (the one fitted curve)  scalar
```

with `trips = n_row·(n_col − T + 1)`. Three of the four terms are *not fitted*:

- **Channel occupancy is deterministic.** Each transferred word is one bus beat at `II=1`, so the
  X-read / Y-write occupancy is simply the word count (`+` a 2-cycle per-burst address setup). The port
  carries this as a `bus_timing`, and the slice calls supply only `num_trans = n_row`. (See
  [transfer-beat occupancy](../../guide/timing/aximm.md#true-channel-occupancy-count-transfer-beats).)
- **Compute is exactly II=1.** `compute_body = trips + (n_row−1)·row_depth` — the MAC sustains one
  output per cycle (validated to R²=1.0 on single-row points).
- **`fill_const` is one scalar** — the command→first-bus latency and drain edge.

The **only** fitted term is **`row_depth(n_col)`** — the per-row pipeline / 2-buffer ping-pong refill
depth — carried as a saturating [`InterpCalibModel`](../../guide/calib/models.md) lookup, *not* a
basis-function fudge. How it (and `fill_const`) are measured and fit is [Fitting the model](./fit.md).

## The store hides under compute

The key timing trick is on the store: the Y-write is **early-anchored** at the time the first output
row is ready, and given a floor of `compute_body`, so it occupies the write channel for
`max(write_occ, compute_body)`. When compute is the bottleneck, the store finishes *under compute's
shadow* — overlapping the X-read instead of running after it. Successive stores serialize by their
*effective* spans, so back-to-back throughput stays correct. This is `max(load, compute, store)` made
explicit on the store side.

## Where the events are logged

To validate against RTL you compare *event timelines*, so each stage emits a timestamped event. The
rule: **a `begin`/`end` pair around every bus-visible transfer**, plus the command arrival as the
anchor.

| event | logged at | bus-visible in cosim? |
|---|---|---|
| `cmd_arrive` | the loader pulls the command | the anchor (t = 0) |
| `load_begin` / `load_end` | around the X-read | **yes** — the X-read span |
| `comp_begin` | compute starts | no — sim-internal |
| `store_begin` / `store_end` | around the Y-write | **yes** — the Y-write span |
| `resp_sent` | the response burst | **yes** |

The bus-visible events are exactly what the [VCD extractor](./cosim_timing.md) recovers from cosim, so
both timelines are anchored at `cmd_arrive` and compared offset-by-offset — the comparison the
[calibration](./fit.md) gates report.

## See also

- [Double-buffered processing](../../guide/concurrency/python/sob.md) — the general model this realizes.
- [Calibration](../../guide/calib/) — `CalibDataFrame` + the models (`row_depth` is an `InterpCalibModel`).
- [Extracting timing from cosim](./cosim_timing.md) / [Fitting the model](./fit.md) — measuring and fitting these terms.
