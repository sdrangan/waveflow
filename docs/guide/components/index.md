---
title: Hardware Components
parent: Guide
nav_order: 6
has_children: true
audience: python
api: [Component, HwComponent, add_endpoint]
summary: "The Python component model, in three layers: a SimObj is anything the simulation schedules (hosts, drivers, testbenches, hardware); a Component is a SimObj with structure — typed endpoints plus a sub-component hierarchy; and a HwComponent is a Component that is hardware, adding the synthesis surface (HwParam template parameters + a codegen identity) so the same class is both the sim model and the source for a generated C++ kernel. Covers defining a component, the execution-model taxonomy and its three kinds (host-activated / free-running / composite), and parameterization."
---
# Hardware Components

## From `SimObj` to `HwComponent`

Everything in a Waveflow design is a [`SimObj`](../sim/) — anything the [simulation](../sim/) schedules,
with the three-phase lifecycle (`pre_sim` → `run_proc` → `post_sim`) and its own concurrent
process(es). Hosts, DMA models, drivers, sinks, and testbenches are all `SimObj`s, and so is every piece
of hardware.

A [`Component`](../../../waveflow/hw/component.py) is a `SimObj` with **structure**: it exposes typed
**endpoints** — the ports it talks to the outside world through — and it can contain **sub-components**
wired together by internal **interfaces**. It is the *connectable node* in the design graph, added to
with `add_endpoint` / `add_comp` / `add_if`.

A [`HwComponent`](../../../waveflow/hw/hw_component.py) is a `Component` that represents **hardware**. On
top of a `Component`'s ports and hierarchy it adds the **synthesis surface** —
[`HwParam`](./parameterization.md) template parameters and a codegen identity (`cpp_kernel_name`,
`control_mode`). That is what makes it the **single source of truth for a hardware block**: the *same
class* is both the model you simulate and the source for a [generated C++ kernel](../comp_codegen/).
Whether a given `HwComponent` is actually generated is a separate axis — most are **synthesizable**,
while others are purely **behavioral** models of hardware Waveflow does not generate (a data converter, a
memory, an RF channel). The [Component taxonomy](./taxonomy.md) classifies the kinds.

## A component is defined by three things

Whatever its realization, a `HwComponent` is defined by three things, and this chapter is organized
around them:

- **Its interface endpoints** — the typed ports it talks to the outside world through (a stream
  input, a memory-mapped master, an AXI-Lite register map). You declare them on the class.
- **How it is wired** — a component does not call other components directly; its endpoints are
  **bound** to [interfaces](../interface/), which carry transactions to the endpoints of other
  components.
- **What it does** — its behavior is expressed as the **methods on those endpoints** (`get` an
  incoming transaction, `write` an outgoing one), driven from its lifecycle methods.

This section is **Python-only** by design — the model you simulate. The same component's
**synthesizable C++ realization** (the generated kernel function, its ports, parameterization) is the
[Component Code Generation](../comp_codegen/) chapter; each page below cross-links its codegen dual.

## In this section

- [Defining a component](./overview.md) — the `HwComponent` class and how you declare its endpoints (`__post_init__` + `add_endpoint`), walked through `simp_fun`.
- [Component taxonomy](./taxonomy.md) — the class tree and the three execution-model kinds at a glance.
- [Host-activated components](./hostactivated.md) — `HostActivated`: regmap-launched, `on_start`, runs once per trigger.
- [Free-running components](./freerun.md) — `FreeRunComp`: a continuous loop, `run_iter`.
- [Composite components](./composite.md) — `CompositeComp`: a bodyless hierarchy whose children do the work.
- [Parameterization](./parameterization.md) — `HwParam` (per-instance synthesis knobs) vs. `HwConst` (class-level structural constants), and `param_supports` variant kernels.

## See also

**Prerequisites** — a component is built from these:

- [Interfaces](../interface/) — the typed ports (stream / MM / regmap endpoints) a component declares, the master/slave roles, and their transaction-method signatures.
- [Data Schemas](../schema/) — the payloads those ports carry.

**The synthesizable side** (the C++ realization of a component):

- [Component Code Generation](../comp_codegen/) — what a full component generates: the kernel structure, how `HwParam` becomes C++ template parameters, and `main()`→testbench emission.
- [Custom Hooks](../custom_hooks/) — the hand-written synthesizable kernel bodies that plug into a generated component.
