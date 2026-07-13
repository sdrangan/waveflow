---
title: Python
parent: Concurrency
nav_order: 1
has_children: true
audience: python
summary: "The Python side of composite systems: building a hierarchical HwComponent from concurrent sub-components wired by internal interfaces, the load-compute-store task-network pattern, stream-of-blocks (ping-pong) channels, multi-input stages, and where per-stage timing lives."
---
# Concurrency — the Python model

A **composite** is a hierarchical [`HwComponent`](../../components/): it declares sub-components and
wires their endpoints together with internal [interfaces](../../interface/), and each sub-component's
`run_proc` runs as a concurrent process in the simulation. This half of the section is how you build one
in Python.

## In this section

- [Sub-components and stream wiring](./subcomponent.md) — adding sub-components to a composite and connecting them with internal stream interfaces.
- [SOB as a modeling pattern](./sob.md) — the stream-of-blocks (ping-pong) channel: a whole-block hand-off for resident, randomly-addressed data.
- [Load-compute-store task network](./lcs.md) — the canonical LCS composite, anchored by the interleaver.
- [Multi-input stages](./multiin.md) — a stage that consumes several stream / block inputs.
- [Timing contract](./timing.md) — where per-stage yields go and why steady-state is emergent (stub — pending the LT model).
