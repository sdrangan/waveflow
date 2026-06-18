---
title: Lifecycle
parent: Simulation
nav_order: 2
audience: python
applies_to: [SimObj, HwComponent]
api: [pre_sim, run_proc, post_sim, on_start, sim_only]
summary: "The SimObj lifecycle — every simulation object runs through pre_sim / run_proc / post_sim, driven by Simulation.run_sim(). on_start (the regmap-launched kernel entry) and @sim_only (excludes a helper from synthesis) are the synthesizable-component specifics, cross-linked to comp_codegen."
---

# Lifecycle

## Concept

The lifecycle is the **`SimObj`** lifecycle. *Every* object in a simulation — a
[`HwComponent`](../components/), an [interface](../interface/), a [`Logger`](./logging.md), a sensor
or channel — is a [`SimObj`](./simobj.md), and [`Simulation.run_sim()`](../../../waveflow/simulation/simulation.py)
drives them all through the same three phases, in registration order:

1. **`pre_sim()`** — setup / validation before the event loop (bind checks, address ranges, initial state).
2. **`run_proc()`** — the object's optional SimPy generator process is scheduled; an object whose
   `run_proc` returns `None` is *passive* (it participates only via `pre_sim` / `post_sim`).
3. **`post_sim()`** — collect results, assert invariants, emit reports, after the event loop ends.

If the run raises, `error_cleanup()` is called on every object before the exception propagates (so
files/loggers close). This page is the **per-object** view — what each hook means for one object; the
*system* view (how `run_sim` drives all objects at once) is the [Simulation](./index.md) index.

## API

- [`SimObj.pre_sim`](../../../waveflow/simulation/simobj.py)
- [`SimObj.run_proc`](../../../waveflow/simulation/simobj.py)
- [`SimObj.post_sim`](../../../waveflow/simulation/simobj.py)
- [`SimObj.error_cleanup`](../../../waveflow/simulation/simobj.py)

## The synthesizable-component specifics

A [`HwComponent`](../components/) is a `SimObj`, so it inherits this lifecycle unchanged — but two of
its methods carry extra meaning on the codegen side:

- **`on_start`** — a component that declares a [`VitisRegMapMMIFSlave`](../components/endpoints.md) is
  *regmap-launched*: instead of a free-running `run_proc`, the host writes `ap_start` and the kernel
  runs `on_start` once. In simulation `on_start` is just the process body; how the generator picks it
  as the kernel entry (vs. `run_proc`) is [Component Code Generation: Component structure](../comp_codegen/structure.md).
- **`@sim_only`** — marks a helper method as **simulation-only** so the synthesis
  [extractor](../comp_codegen/extractor.md) excludes it from the lowered kernel. It is inert in the
  Python run; its whole purpose is the codegen boundary.

- [`@sim_only`](../../../waveflow/hw/synth.py) — the marker.

## Example

From [`examples/stream_inband/poly.py`](../../../examples/stream_inband/poly.py),
`PolyAccelComponent` defines `on_start` for its regmap-triggered kernel entry and a `@sim_only` helper
(`_inc_job`) that is excluded from synthesis extraction.

## Quick reference

- `pre_sim`: setup / validation before the event loop (every `SimObj`).
- `run_proc`: the object's process body; return `None` to be passive.
- `post_sim`: final checks / reporting after the loop.
- `on_start`: the regmap-launched kernel entry for a `HwComponent` (see [structure](../comp_codegen/structure.md)).
- `@sim_only`: keep a helper out of synthesis (see [extractor](../comp_codegen/extractor.md)).
