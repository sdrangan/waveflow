---
title: HLS
parent: Concurrency
nav_order: 2
has_children: true
audience: hls
summary: "The HLS side of composite systems: how a hierarchical HwModule lowers to a free-running hls::task network, the two realizations of load-compute-store (single-kernel DATAFLOW vs. multi-component task network), stream-of-blocks synthesis, and the composite codegen."
---
# Concurrency — the HLS realization

How a composite is generated into Vitis: a free-running (`ap_ctrl_none`) `hls::task` network — one task
per sub-component — wired by internal streams and stream-of-blocks channels.

## Two realizations of load-compute-store

Waveflow realizes a load-compute-store accelerator as a **multi-component task network** — a generated
`hls::task` network from a hierarchical composite (the interleaver path documented here). Each stage
(load / compute / store) is its own component, wired by streams and stream-of-blocks channels. (An
earlier single-kernel `#pragma HLS DATAFLOW` path has been retired in favor of this composition.)

The task-network realization is the **canonical direction going forward**; the single-kernel DATAFLOW
page remains valid and is kept for existing single-accelerator flows.

## In this section

- [Synthesis types and caveats](./synth_types.md) — which top-levels compose into a free-running network, and the `hls::task` + `m_axi` cosim caveat.
- [Sub-component lowering to `hls::task`](./hlstask.md) — how each sub-component becomes a free-running task.
- [SOB synthesis / performance notes](./sob.md) — `stream_of_blocks` and the gather/scatter throughput asymmetry.
- [Composite codegen wiring](./codegen.md) — generating the multi-task top.
