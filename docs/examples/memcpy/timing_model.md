---
title: Timing models
parent: Memory Copy
nav_order: 8
---

# Timing models

The [previous page](./timing.md) showed the pysim reproducing the RTL's 183-cycle period. That accuracy
comes from **two timing models**, both attached to framework components `mem_copy` reuses:

- a **bus model** — how long the `m_axi` interconnect takes to move the words, attached to the memory;
- a **mem-stream model** — each stream owner's own control cost, attached to `MemRStream` / `MemWStream`.

Both are *infra*, so Waveflow **ships their fitted parameters for supported platforms** — `mem_copy`
loads them and needs no fitting of its own. This page describes the models and how they plug in; the
[next page](./timing_fitting.md) shows how to fit them from scratch for a new platform.

## The shape of a firing

Sweep `n_words` and the writer's firing span (its `ap_done`-anchored occupancy) is, to the cycle:

```
span = latency  +  n_words  +  burst_time · (num_trans − 1)
       └ control ┘  └──────── bus transfer ────────┘
                                 num_trans = ceil(n_words / 16)
```

For the writer at `n_words = 128`: `41 + 128 + 2·(8 − 1) = 183`. Three physically distinct terms:

- **`n_words`** — one cycle per word moved (the datapath runs at `II = 1`).
- **`burst_time · (num_trans − 1)`** — the gap the `m_axi` adapter pays at each burst boundary (HLS's
  `max_read/write_burst_length` is 16, so `n` words issue as `ceil(n/16)` bursts). `burst_time` is 2 for
  the write channel, 1 for the read.
- **`latency`** — the stream owner's own fixed control cost (~41 for the writer), independent of size.

The first two terms are the **bus transfer** — a property of the platform's memory system, the same for
every accelerator. The last is the **component's control** — its own overhead. Splitting the span this
way is exactly the [two-level split](../../guide/calib/) of the calibration system.

## Model 1 — the bus (attached to the memory)

The bus transfer is charged by a [`BusTiming`](../../guide/calib/bus_model.md) on the memory's `m_axi`
slave. The testbench loads it from the platform and hands it to the memory:

```python
# examples/mem_copy/mem_copy_sim.py — MemCopyTB
self.mem.s_mm.bus_timing = BusCalib(self.platform_dir, clk_freq=self.clk.freq).bus_timing()
```

Now every burst a mem-stream issues through that slave is charged `n_words + burst_time·(num_trans − 1)`
cycles — the platform's measured `m_axi` law, not a guess. On a platform with no calibration the
`BusTiming` degrades to a plain per-word span (no crash), just less accurate.

## Model 2 — the mem-stream control (attached to the component)

Each stream owner carries its own [`StreamTimingModel`](../../guide/calib/component_residual.md), which
predicts the control cost and charges it in the firing. `MemRStream` / `MemWStream` take a `platform_dir`
(the shared library) or a `calib_dir` (a project-local path); the model is attached in `__post_init__`
and its delay is applied in `run_iter` via `timed_delay`:

```python
# waveflow/hw/mem_stream.py — the writer's run_iter, after the store
dly = self.timed_delay({"nwords": nw, "num_trans": math.ceil(nw / 16)})  # predict + record
if dly:
    yield self.timeout(dly)
```

`timed_delay` both **predicts** the delay and **records** the firing, so the same hook that applies the
model during a normal run also collects the datapoints that [fit](./timing_fitting.md) it. With no model
attached it returns `0.0` — the components are uncalibrated by default and only carry timing when a
platform is supplied. (The general pattern is
[Adding a timing model](../../guide/timing_model/insertion.md).)

> **Why the mem-stream model fits a *residual*, not the whole 41.** The pysim already accounts for part
> of the control cost (the pipeline fill, the overlapped drain). So the `StreamTimingModel` adds only the
> gap between the RTL and the pysim — about 22 cycles at `n = 128`, not the full 41 — the
> [residual](../../guide/calib/component_residual.md) the fitting page recovers.

## Measure on the *uncontended* firings

One subtlety the [visualization](./timing.md#the-per-firing-table) exposed: the reader's steady-state
firings each carry ~30 grey cycles of *waiting* on a still-draining writer. That congestion is **not** a
property of the reader — it **emerges** in the simulation from a bounded `copy_data` FIFO. So a model is
fit only on **uncontended** firings (`blocked == 0`): measure each component's own cost in isolation, and
let the sim reproduce the contention. Feed the two clean models back, give the internal channels their
real depth of 2, and the 30 cycles of congestion re-emerge — reproducing the RTL across a 16× size range
to ~1%, versus 27% for a single-point fit that baked the contention in.

## See also

- [Visualizing timing](./timing.md) — the run these models reproduce.
- [Fitting the models](./timing_fitting.md) — the sweep, the fit, and the platform library that stores
  the result.
- [Timing model fitting](../../guide/calib/) — the general two-level calibration system these are an
  instance of.
