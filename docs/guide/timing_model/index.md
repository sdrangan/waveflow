---
title: Timing Models
parent: Guide
nav_order: 12.5
has_children: true
audience: python
api: [TimedPhase, HwModule.add_timing_phase, TimedPhase.timeout, TimedPhase.pipeline, LinearTiming, BusCalib, StreamTimingModel]
summary: "How a module says how long its work takes, and how those numbers are recovered from measurement. A module declares timed phases -- spans between events at its own ports, a function of features, learned by a model -- and its body waits them: one span for a sequential body, two (latency, fill) for a pipelined one. Calibrating them is generated from the declaration: the RTL transfers on the named ports, paired with pysim's records, fitted per span. The transfers time themselves; the platform's bus model and the framework's mem-stream residuals cover what modules do not declare."
---

# Timing Models

Every `HwModule` carries two models.  The **functional** model says *what* it computes; the
**timing** model says *how long* that takes -- when, in simulated time, the work finishes.

Most of the time you do not write one.  **The transfers time themselves**: a stream `get` / `write`,
a memory read, a bus transaction advance simulated time by how long the transfer takes, and a stall
lengthens them on its own.  What the framework cannot know is how long **your compute** takes -- the
body of a [custom hook](../custom_hooks/) -- and that is what a module declares.

## Declare it

A module states its timing as **[timed phases](./timed_phases.md)**: spans between events at its own
ports -- the first or last beat of a transfer -- each a function of features known before it starts,
predicted by a model.  The figure on that page shows the polynomial kernel's three spans on a real RTL
trace.

- [What a timed phase is](./timed_phases.md) -- events, spans, features, the two rules, on a trace.
- [LT vs CT models](./models.md) -- why a transfer is one event, not one per cycle: the bet that makes a
  whole-system Python simulation fast.
- [Declaring a timed phase](./insertion.md) -- `TimedPhase`, `add_timing_phase`, the rules a
  declaration is held to.
- [Sequential bodies](./block.md) -- the whole input, then the compute: one span,
  `yield phase.timeout(...)`.
- [Pipelined bodies](./streaming.md) -- compute while the input arrives: a latency and a fill,
  `phase.pipeline(...)`.
- [The form of a span's model](./loops.md) -- a pipelined loop costs `latency + ii·(m − 1)`, and how
  that maps onto the spans.

## Calibrate it

- [Calibrating a timed phase](./fit.md) -- generated from the declaration: the RTL transfers on the
  named ports, paired with pysim's records, fitted per span; poly re-fitted from its committed trace.

What modules do not declare is timed by the platform and the framework, calibrated once and reused:

- [The bus-transfer model](./bus_model.md) -- how long the interconnect takes to move `n` words in `k`
  bursts: a property of the **platform**, fit once per platform and reused by every accelerator.
- [Component residuals](./component_residual.md) and [the mem-stream residual](./memstream.md) -- how the
  framework's memory-mapped stream blocks were calibrated before timed phases: the gap between RTL and
  pysim, fit per `(component, platform)`.  They keep that method until they are declared as phases.
- [Polling Overhead](./poll.md) -- the loosely-timed model of `MMIFMaster.poll_until`: the bandwidth a
  poll loop steals from its bus and the discovery latency.

Software is timed the same way, on a processor rather than a component: a
[processor model](../cpu/index.md) prices a Python function's work counters with models calibrated
on gem5.

### The two-level split: bus vs component {#the-two-level-split-bus-vs-component}

For a component that moves data over `m_axi`, the run's cost splits:

```text
    RTL cycles  =  bus transfer  +  component control
                    └─ PLATFORM ─┘   └── COMPONENT ──┘
```

The **bus transfer** -- how long the interconnect takes to move `n` words in `k` bursts -- is a property
of the **platform** (memory system + AXI adapter), so it is fit **once per platform** and reused by
every accelerator ([`BusCalib`](./bus_model.md)).  With that charged in pysim, what is left is the
component's own time -- a timed phase, or for the framework's mem streams, a fitted residual
([`StreamTimingModel`](./component_residual.md)).  A new accelerator on a calibrated platform inherits
the bus term for nothing and only has to calibrate what is genuinely its own.

### Everything is stored in cycles

Fitted timing numbers are **cycles**, not seconds, so the artifact is clock-independent: re-deploying
at a different *simulation* frequency needs no refit.  The clock that does change them is the
**synthesis** clock, which is why a [platform](../platform/identity.md) is keyed by part *and* period.

The machinery underneath -- the model base, the corpus format, the confidence levels -- is
axis-agnostic and lives in [Model calibration](../calib/).  Resource models use the same base; only
the source of a number differs.

## See also

- [Simulation timing model](../sim/timing.md) -- the `Clock`, `self.timeout`, and where transfer vs.
  compute latency is charged.  This section assumes that page.
- [Model calibration](../calib/) -- the shared `CalibModel`, corpus and confidence the calibration half
  is built on.
- [Timing Analysis Tools](../timing/) -- the *measurement* side: cycle counts and port transfers from a
  VCD / cosim run, which is where the datapoints come from.
- [Custom hooks](../custom_hooks/) -- the hand-written compute whose timing you declare here.
