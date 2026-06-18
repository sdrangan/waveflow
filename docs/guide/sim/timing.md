---
title: Timing model
parent: Simulation
nav_order: 4
audience: python
api: [Clock, Clock.period, SimObj.now, SimObj.timeout, SimObj.action]
summary: "The forward timing model that produces the simulated timeline — Clock (frequency / period), how interfaces charge transfer latency in cycles, and how a component models compute latency with self.timeout / self.action. (Analyzing the result is Timing Analysis.)"
---

# Timing model

A discrete-event simulation has a clock on the wall: `self.now` is the current time in **seconds**.
This page is the **forward model** — how that time advances: the `Clock`, how interfaces charge for a
transfer, and how a component charges for its own compute. *Analyzing* the resulting timeline
(throughput, latency, overlap) is the separate [Timing Analysis Tools](../timing/) section.

## Clock

A [`Clock`](../../../waveflow/hw/clock.py) is a timing domain — just a frequency:

```python
from waveflow.hw.clock import Clock

clk = Clock(freq=100e6)   # 100 MHz
clk.period                # 1e-8  seconds  (== 1.0 / freq)
```

A `Clock` is passed to each `Interface` object (and usually held on components as a `clk` field). It
converts **cycle counts** — the natural unit for hardware — into the **seconds** the SimPy
environment advances in.

## Interfaces charge transfer latency

An `Interface` owns the latency model for data crossing it. The standard model is a fixed setup cost
plus one cycle per word:

```
transfer_time = (latency_init + nwords) / clk.freq   [seconds]
```

`latency_init` captures wire delay / arbitration; each word adds one beat. So `yield`ing a
`master.write(words)` blocks the caller for `transfer_time` of simulated time. The per-interface
parameters (`latency_init`, the FULL/LITE read/write formulas, `latency_per_word`) are documented
with each interface — see [Overview](../interface/overview.md), [streams](../interface/stream.md),
and [memory-mapped](../interface/aximm.md). This page only notes *that* interfaces are where transfer
time is charged.

## Components charge compute latency

Transfer time alone is not the whole timeline — a component also spends time **computing**. Two
ways to model that, both on [`SimObj`](../../../waveflow/simulation/simobj.py):

**Wait explicitly with `self.timeout(delay)`** — `delay` in seconds, so convert cycles via the clock:

```python
def run_proc(self) -> ProcessGen[None]:
    # ... read input ...
    yield self.timeout(compute_cycles / self.clk.freq)   # model the compute latency
    # ... write output ...
```

**Or wrap a window with `self.action(name, processing_delay)`** — it advances time by
`processing_delay` *and* records the window for analysis (start/end), so overlaps can be detected:

```python
yield from self.action("decode", processing_delay=3 / self.clk.freq)
```

Each call appends an `ActionRecord(name, start, end)` to `self.action_history`; concurrent windows
on the same object are collected in `self.action_overlaps` (count via
`self.active_overlap_count()`). `self.now` is the current time at any point.

### Pipelined transfers

For a component that streams data through (rather than buffering a whole burst), the stream endpoints
expose `get_pipelined` / `write_pipelined`, which carry the first-word arrival time and the
component's initiation interval / latency (`proc_ii`, `proc_latency`, set to match HLS synthesis
numbers). That mechanism is documented on the [stream interface](../interface/stream.md) page.

## What this feeds

These cycle costs are what make the simulated timeline meaningful — and what the
[Timing Analysis Tools](../timing/) then measure (and what the [Logger](./logging.md) records as
timestamped events for later analysis).

## See also

- [Logging](./logging.md) — capture `self.now`-stamped events to a CSV during the run.
- [Timing Analysis Tools](../timing/) — analyzing the timeline this model produces.
- [Interface Overview](../interface/overview.md) — the per-interface latency parameters.
