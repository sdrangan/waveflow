---
title: Hardware Components
parent: Guide
nav_order: 5
has_children: true
audience: python
summary: "The Python HwComponent model — declaring typed ports/endpoints, HwParam/HwConst fields, and the pre_sim/run_proc/post_sim lifecycle in the SimPy simulation."
---

# Hardware Components

This section is the **Python `HwComponent` model**: how you declare a hardware component in the SimPy simulation — its typed ports (endpoints), its `HwParam` / `HwConst` fields, and its lifecycle. It is Python-only by design; a component's **synthesizable C++ realization** — the generated kernel structure, parameterization, and hand-written hooks — is the codegen arc (see the forward-pointers below).

## Concept

`Component` is the base simulation object with named endpoints and SimPy lifecycle hooks. `HwComponent` extends it with synthesis-aware semantics: extractor-compatible methods, hardware endpoint declarations, and codegen metadata.

Within a component class, fields usually fall into three categories:

- `HwConst[T]` class-level constants for compile-time-style values.
- `HwParam[T]` instance parameters that participate in synthesis templating.
- Plain Python fields for simulation-only state and runtime configuration.

Endpoints are declared in `__post_init__` and attached with `add_endpoint(...)`, typically including stream interfaces (`StreamIFMaster` / `StreamIFSlave`) and AXI-Lite control through regmap-backed endpoints such as `VitisRegMapMMIFSlave`. AXI-MM style interfaces are documented in [Interfaces](../interface/aximm.md).

## API

- [`Component`](../../../waveflow/hw/component.py)
- [`HwComponent`](../../../waveflow/hw/hw_component.py)
- [`add_endpoint(endpoint)`](../../../waveflow/hw/component.py)
- [`HwParam`](./hwparam.md)
- [`HwConst`](./hwconst.md)
- [`HwTestbench`](./hwtestbench.md)

## Example

From [`examples/stream_inband/poly.py`](../../../examples/stream_inband/poly.py), `PolyAccelComponent` declares stream + regmap endpoints in `__post_init__` and registers each endpoint through `add_endpoint(...)`.

## Quick reference

- Use `Component` for simulation-only behavior.
- Use `HwComponent` for synthesizable designs.
- Declare endpoints explicitly in `__post_init__`.
- Keep synthesis knobs in `HwParam` fields.
- Keep compile-time constants in `HwConst` fields.

## In this section

- [HwParam](./hwparam.md)
- [HwConst](./hwconst.md)
- [HwTestbench](./hwtestbench.md)
- [Lifecycle](./lifecycle.md)

## See also

**Prerequisites** — a component is built from these:

- [Interfaces](../interface/) — the typed ports (stream / MM / regmap endpoints) a component declares.
- [Data Schemas](../schema/) — the payloads those ports carry.

**The synthesizable side** (the C++ realization of a component):

- [Component Code Generation](../comp_codegen/) — what a full component generates: the kernel structure, how `HwParam` becomes C++ template parameters, and `main()`→testbench emission.
- [Custom Hooks](../custom_hooks/) — the hand-written synthesizable kernel bodies that plug into a generated component.
