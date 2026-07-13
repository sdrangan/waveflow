---
title: Hardware Components
parent: Guide
nav_order: 6
has_children: true
audience: python
api: [HwComponent, Component, add_endpoint]
summary: "The Python HwComponent model: a module in a larger simulation — synthesizable, testbench, or purely behavioral — defined by its interface endpoints, wired to other components by binding endpoints to interfaces, with behavior expressed as the methods on those endpoints. Covers the component taxonomy, defining a component, the endpoint methods, and HwParam / HwConst / HwTestbench (its lifecycle lives in Simulation)."
---
# Hardware Components

A [`HwComponent`](../../../waveflow/hw/hw_component.py) is Waveflow's representation of a basic **module** in a larger simulation.  Every component runs in the [SimPy simulation](../sim/); what differs is its **realization**. Most are **synthesizable** — the same class is *also* the source for a [generated C++ kernel](../comp_codegen/) — but a component can also be a
**testbench**, or a purely **behavioral** model of hardware Waveflow does not generate (a data converter,
a memory, an RF channel). The full classification is [Component taxonomy](./taxonomy.md).

Whatever the type of component -- synthesizable, testbench, or purely behavioral -- the component is defined by three things, and this chapter is organized around them:

- **Its interface endpoints** — the typed ports it talks to the outside world through (a stream
  input, a memory-mapped master, an AXI-Lite register map). You declare them on the class.
- **How it is wired** — a component does not call other components directly; its endpoints are
  **bound** to [interfaces](../interface/), which carry transactions to the endpoints of other
  components.
- **What it does** — its behavior is expressed as the **methods on those endpoints** (`get` an
  incoming transaction, `write` an outgoing one), driven from its lifecycle methods.

This section is **Python-only** by design — the model you simulate. The same component's
**synthesizable or testbench C++ realization** (the generated kernel function, its ports, parameterization) is the
[Component Code Generation](../comp_codegen/) chapter; each page below cross-links its codegen dual.

## In this section

- [Defining a component](./overview.md) — the `HwComponent` class and how you declare its endpoints (`__post_init__` + `add_endpoint`), walked through `simp_fun`.
- [Component taxonomy](./taxonomy.md) — the kinds of component (synthesizable / testbench / behavioral) and how each is realized per build target.
- [Endpoint methods](./endpoints.md) — the master/slave roles and the method you define or call per endpoint type (stream, m_axi, regmap, schema/array transfer).
- [Parameterization](./parameterization.md) — `HwParam` (per-instance synthesis knobs) vs. `HwConst` (class-level structural constants), and `param_supports` variant kernels.
- [HwTestbench](./hwtestbench.md) — a component subclass whose `main()` is a Python test sequence.

## See also

**Prerequisites** — a component is built from these:

- [Interfaces](../interface/) — the typed ports (stream / MM / regmap endpoints) a component declares, and their transaction-method signatures.
- [Data Schemas](../schema/) — the payloads those ports carry.

**The synthesizable side** (the C++ realization of a component):

- [Component Code Generation](../comp_codegen/) — what a full component generates: the kernel structure, how `HwParam` becomes C++ template parameters, and `main()`→testbench emission.
- [Custom Hooks](../custom_hooks/) — the hand-written synthesizable kernel bodies that plug into a generated component.
