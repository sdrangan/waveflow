---
title: Timing Models
parent: Guide
nav_order: 12.5
has_children: true
audience: python
api: [FreeRunMod.add_timing_model, TimingModel, StreamTimingModel, BusCalib, LinCalibModel, SimObj.timeout]
summary: "How a component says how long its work takes, and how those numbers are recovered from measurement. Two halves: DECLARING the form (LT vs CT, attaching a model, the latency + ii(m-1) loop model, block vs streaming insertion) and CALIBRATING it (the direct sweep fit, the RTL-vs-pysim residual, the once-per-platform bus model, and the shipped mem-stream residual). Most Waveflow operations already carry a built-in timing model, so in practice you model a custom hook's compute."
---

# Timing Models

Every `HwModule` carries two models. The **functional** model says *what* it computes; the
**timing** model says *how long* that takes — when, in simulated time, the work finishes.

Most of the time you don't write one. **Most Waveflow operations already carry a built-in timing
model:** when you `yield from self.mem_if.read_array(...)`, the simulation advances the clock by an
estimate of how long that transfer takes; a stream `get` / `write` does the same. Those are the costs
the framework already knows.

What the framework *cannot* know is how long **your compute** takes — the body of a
[custom hook](../custom_hooks/). So in practice a user builds a timing model **only for the compute of a
custom hook**: a small model that expresses the **elapsed cycles as a function of the input size** — the
number of samples, the vector length — the parameter the work scales with.

## Declare it, then calibrate it

The section is in two halves, and they are the same model at two stages of its life.

**Declaring** a model fixes its *form* — a pipelined loop costs `latency + ii·(m − 1)` cycles, and
that shape is a property of the hardware you wrote. **Calibrating** it recovers the *numbers* in that
form from measurement, so the fast LT simulation reproduces what the RTL actually did.

A declared-but-uncalibrated model still simulates: it runs on its seed and says
[`UNCALIBRATED`](../calib/confidence.md), which is the honest state for a design that has not been
synthesized yet.

### Declaring — the form

- [LT vs CT models](./models.md) — loosely-timed vs cycle-timed simulation, and why Waveflow is LT
  (the bet that justifies modeling a transaction's timing rather than every cycle).
- [Adding a timing model to a component](./insertion.md) — where a timing model plugs in: attach it,
  and charge the delay it predicts with `self.timeout`.
- [Timing models for loops](./loops.md) — the typical compute model: a pipelined loop costs
  `latency + ii·(m − 1)` cycles — linear in two parameters, expressed as a `LinCalibModel`.
- [Block processing](./block.md) — inserting the model in a **block** process: the compute runs *after*
  the whole block has loaded (the prediction goes after the load, before the store).
- [Streaming processing](./streaming.md) — inserting the model in a **streaming** process: the compute
  overlaps the load and/or store, element by element.

Double-buffering (ping-pong overlap) is no longer a separate timing model: it is built by composing
**load / compute / store as concurrent sub-components over a [stream of blocks](../interface/sob.md)**,
and the compute sub-component is timed exactly like a [block](./block.md) process.

### Calibrating — the numbers

Two methods, and which one you want depends on how much of the cost the LT sim *already* charges:

- [Fitting a timing model](./fit.md) — the **direct** method: fit the parameters straight from a sweep
  of `(size, cycles)`. A loop model's `latency` / `ii` are the two coefficients of a line. Reach for
  this when the model owns the whole cost.
- [Component residuals](./component_residual.md) — the **residual** method: when the interfaces
  already time the transfers, fit only the *gap* between RTL and pysim — the control overhead pysim
  misses. `StreamTimingModel`, fit per `(component, platform)`.
- [The bus-transfer model](./bus_model.md) — `BusCalib`: how long the interconnect takes to move `n`
  words in `k` bursts is a property of the **platform**, so it is fit once and reused by every
  accelerator. Charging it in pysim is what shrinks the component residual to the kernel's own cost.
- [The mem-stream residual](./memstream.md) — the reusable `MemRStream` / `MemWStream` control
  residual and the fixture that fits it. Ships calibrated, so a design on a known platform inherits
  it with no re-calibration.

### The two-level split: bus vs component {#the-two-level-split-bus-vs-component}

For a component that moves data over `m_axi`, the residual method leans on a split. The run's cost is:

```text
    RTL cycles  =  bus transfer  +  component control
                    └─ PLATFORM ─┘   └── COMPONENT ──┘
```

The **bus transfer** — how long the interconnect takes to move `n` words in `k` bursts — is a property
of the **platform** (memory system + AXI adapter), so it is fit **once per platform** and reused by
every accelerator ([`BusCalib`](./bus_model.md)). With that charged in pysim, the **component control**
residual shrinks to the kernel's own overhead, fit per `(component, platform)`
([`StreamTimingModel`](./component_residual.md)).

That split is what makes the second level cheap. A new accelerator on a calibrated platform inherits
the bus term for nothing and only has to fit what is genuinely its own.

### Everything is stored in cycles

Fitted timing numbers are **cycles**, not seconds, so the artifact is clock-independent: re-deploying
at a different *simulation* frequency needs no refit. The clock that does change them is the
**synthesis** clock, which is why a [platform](../platform/identity.md) is keyed by part *and* period.

The machinery underneath both methods — the model base, the corpus format, the confidence levels — is
axis-agnostic and lives in [Model calibration](../calib/). Resource models use the same base; only
the source of a number differs.

## See also

- [Simulation timing model](../sim/timing.md) — the `Clock`, `self.timeout`, and where transfer vs.
  compute latency is charged. This section assumes that page.
- [Model calibration](../calib/) — the shared `CalibModel`, corpus and confidence this section's
  calibration half is built on.
- [Timing Analysis Tools](../timing/) — the *measurement* side: extracting cycle counts and bus spans
  from a VCD / cosim run, which is where the datapoints come from.
- [Custom hooks](../custom_hooks/) — the hand-written compute whose timing you model here.
