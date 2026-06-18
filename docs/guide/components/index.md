---
title: Hardware Components
parent: Guide
nav_order: 5
has_children: true
audience: python
api: [HwComponent, Component, add_endpoint]
summary: "The Python HwComponent model: a synthesizable hardware module defined by its interface endpoints, wired to other components by binding endpoints to interfaces, with behavior expressed as the methods on those endpoints. Covers defining a component, the endpoint methods, the lifecycle, and HwParam / HwConst / HwTestbench."
---

# Hardware Components

A [`HwComponent`](../../../waveflow/hw/hw_component.py) is Waveflow's representation of a
**synthesizable hardware module**. You write it once in Python, and that one class is the source for
both the SimPy simulation and the [generated C++ kernel](../comp_codegen/).

A component is defined by three things, and this chapter is organized around them:

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
- [Endpoint methods](./endpoints.md) — the master/slave roles and the method you define or call per endpoint type (stream, m_axi, regmap, schema/array transfer).
- [Lifecycle](./lifecycle.md) — `pre_sim` / `run_proc` / `post_sim` and `on_start`: *when* the endpoint methods run.
- [Parameterization](./parameterization.md) — `HwParam` (per-instance synthesis knobs) vs. `HwConst` (class-level structural constants), and `param_supports` variant kernels.
- [HwTestbench](./hwtestbench.md) — a component subclass whose `main()` is a Python test sequence.

## See also

**Prerequisites** — a component is built from these:

- [Interfaces](../interface/) — the typed ports (stream / MM / regmap endpoints) a component declares, and their transaction-method signatures.
- [Data Schemas](../schema/) — the payloads those ports carry.

**The synthesizable side** (the C++ realization of a component):

- [Component Code Generation](../comp_codegen/) — what a full component generates: the kernel structure, how `HwParam` becomes C++ template parameters, and `main()`→testbench emission.
- [Custom Hooks](../custom_hooks/) — the hand-written synthesizable kernel bodies that plug into a generated component.
