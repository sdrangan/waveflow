---
title: Concurrency
parent: Guide
nav_order: 8.5
has_children: true
audience: python
summary: "Waveflow's concurrency model: hierarchical composite components whose sub-components run in parallel, lowered to free-running hls::task networks. Split into the Python model and its HLS realization."
---
# Concurrency

*Concurrency* is fundamental to hardware: modules run in parallel, and that parallelism is where
hardware's throughput comes from. Waveflow models it directly — each `HwModule` defines a `run_proc`
that spawns a process running in parallel in the simulation.

A component built from **sub-components** wired together is a **composite**: a hierarchical
`HwModule` whose children run concurrently (like `InterleaverCanon` and its six tiles). This section
has two halves:

- **[Python model](./python/)** — how to describe composite systems hierarchically in Python, including
  the important **load-compute-store** dataflow class.
- **[HLS realization](./hls/)** — how composite systems are generated into Vitis as free-running
  `hls::task` networks.

## See also

- [Module structure](../comp_codegen/structure.md) — free-running vs. launched kernel modes.
- [Hardware Modules](../flows/modules.md) and [Interfaces](../interface/) — the Python-side declarations composites are built from.
