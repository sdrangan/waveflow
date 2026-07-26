---
title: Timing Models
parent: Guide
nav_order: 12
has_children: true
audience: python
api: [FreeRunMod.add_timing_model, LinCalibModel, SimObj.timeout, MMIFMaster.read_array, StreamIFSlave.get_pipelined]
summary: "How a component says how long its work takes. Most Waveflow operations already carry a built-in timing model — a read/write advances the clock by an estimate of the transfer — so a user typically only builds a timing model for a custom hook's compute. A timing model expresses the elapsed cycles as a function of the input size (e.g. the number of samples). This section: how to add one to a component, the typical loop model (latency + ii·(m−1) as a LinCalibModel), and how to insert it in a block process (compute after the whole block loads) vs. a streaming process (compute overlaps load/store)."
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

## In this section

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
**load / compute / store as concurrent sub-components over a [stream of blocks](../concurrency/python/sob.md)**,
and the compute sub-component is timed exactly like a [block](./block.md) process.

## See also

- [Simulation timing model](../sim/timing.md) — the `Clock`, `self.timeout`, and where transfer vs.
  compute latency is charged. This section assumes that page.
- [Timing model fitting](../calib/) — recovering a model's parameters (`latency`, `ii`) from
  measurement, so the LT sim tracks the RTL.
- [Custom hooks](../custom_hooks/) — the hand-written compute whose timing you model here.
