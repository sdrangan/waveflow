---
title: Concurrency (Python) — Sub-components
parent: Concurrency
nav_order: 1
audience: python
api: [HwComponent, StreamIF, MemRStream, MemWStream, KernelTask]
summary: "Hierarchical concurrency modeling with reusable sub-components and STREAM interconnects, anchored by MemCopy and the reusable MemRStream/MemWStream pattern."
---

# Concurrency (Python) — Sub-components

Waveflow models concurrency by composing `HwComponent` sub-components and wiring interfaces between
their endpoints.

## Reusable anchor: `MemRStream` / `MemWStream` via `MemCopy`

`examples/interleaver/mem_copy.py` is the reusable composition anchor:

- reusable memory-owner components: `MemRStream` (read owner), `MemWStream` (write owner)
- stream-only sequencer stage
- internal `StreamIF` edges between sub-components

This is the reusable composition story: pre-built pieces wired into a hierarchical parent.

## Why this matters for concurrency

At Python level, concurrency is explicit in structure:

- `add_comp(...)` defines concurrent sub-components.
- `add_if(...)` / edge wiring defines dataflow between them.
- each sub-component keeps its own endpoint ownership contract.

The generated top then lowers this structure to a free-running task network.

## See also

- [`examples/interleaver/mem_copy.py`](../../../../examples/interleaver/mem_copy.py)
- [Load-compute-store task network](./lcs.md) — bespoke interleaver composition.
- [Component structure](../../comp_codegen/structure.md)
