---
title: Composite codegen
parent: HLS
grand_parent: Concurrency
nav_order: 4
audience: hls
api: [MemStreamStep, render_top, composite_top_spec, TopSpec]
summary: "How concurrency codegen is assembled: fixed task headers from MemStreamStep, template instantiation, and generated static/thread-local internal channel wiring."
---

# Concurrency (HLS) — Composite codegen

Task-network codegen is graph-derived and template-based.

## Inputs to codegen

- `MemStreamStep` copies fixed task-body headers (`cmd_rx_task.h`, `il_*_task.h`, mem-stream tasks).
- component graph descriptors (`ordered_subcomps`, `internal_edges`, `boundary`) drive top assembly.
- each child `KernelTask` contributes function/header/signature/template args.

## Generated top shape

`render_top(...)` emits:

- top interface pragmas (including `ap_ctrl_none`)
- internal `hls_thread_local` channels:
  - `hls::stream<...>` for `StreamEdge`
  - `hls::stream_of_blocks<...>` for `SobEdge`
- one `hls_thread_local hls::task` per sub-component

So the standalone mem-stream kernels and the six-stage interleaver use the same generation seam.

## See also

- [`examples/interleaver/mem_stream_gen.py`](../../../../examples/interleaver/mem_stream_gen.py)
- [`waveflow/build/composite_gen.py`](../../../../waveflow/build/composite_gen.py)
- [`waveflow/build/streamutils.py`](../../../../waveflow/build/streamutils.py)
