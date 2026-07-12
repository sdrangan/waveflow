---
title: Concurrency
parent: Guide
nav_order: 8.5
has_children: true
audience: python
summary: "Waveflow concurrency model: hierarchical components lowering to free-running `hls::task` networks, and how that canonical path relates to the older single-kernel `#pragma HLS DATAFLOW` realization."
---

# Concurrency

Waveflow now has two documented load-compute-store realizations:

1. **Single-kernel DATAFLOW** (`#pragma HLS DATAFLOW`) in
   [Custom Hooks: Dataflow](../custom_hooks/dataflow.md).
2. **Multi-component task network** (`hls::task`) generated from hierarchical components
   (the interleaver path).

The task-network realization is the **canonical direction going forward**. The single-kernel DATAFLOW
page remains valid and is kept for existing flows.

## Canonical task-network shape (interleaver)

The generated canonical topology is:

`cmd_rx → il_mem_r → il_load → il_compute → il_store → il_mem_w → s_done`

This is a free-running `ap_ctrl_none` network composed from sub-components and internal edges, then
lowered by composite codegen.

## In this section

### Python modeling

- [Sub-components and stream wiring](./python/subcomponent.md)
- [SOB as a modeling pattern](./python/sob.md)
- [Load-compute-store task network](./python/lcs.md)
- [Multi-input stages](./python/multiin.md)
- [Timing contract (stub)](./python/timing.md)

### HLS realization

- [Synthesis types and caveats](./hls/synth_types.md)
- [Sub-component lowering to `hls::task`](./hls/hlstask.md)
- [SOB synthesis/performance notes](./hls/sob.md)
- [Composite codegen wiring](./hls/codegen.md)

## See also

- [Custom Hooks: Dataflow](../custom_hooks/dataflow.md) — single-kernel LCS.
- [Component structure](../comp_codegen/structure.md) — free-running vs launched kernel modes.
- [Endpoint interfaces](../comp_codegen/interface.md) — stream/memory port lowering.
- [Hardware Components](../components/) and [Interfaces](../interface/) — Python-side declarations.
