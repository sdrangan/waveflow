---
title: Concurrency (HLS) — Synthesis types
parent: Concurrency
nav_order: 6
audience: hls
api: [hls::task, ap_ctrl_none]
summary: "Synthesis model for Waveflow task networks: free-running `ap_ctrl_none` task composition, and the `hls::task` + `m_axi` ownership caveat used by the interleaver path."
---

# Concurrency (HLS) — Synthesis types

Waveflow task-network tops synthesize as free-running kernels:

- top protocol: `#pragma HLS INTERFACE ap_ctrl_none port=return`
- internal execution: `hls_thread_local hls::task ...` instances

## `hls::task` + `m_axi` caveat

In the current pattern, a task that owns `m_axi` should only touch streams/tokens and should not also
hold SOB locks in the same task. The canonical interleaver follows this split:

- `il_mem_r`, `il_mem_w` own `m_axi`
- `il_load`, `il_compute`, `il_store` own SOB lock-scoped block work

This ownership split is structural, not stylistic.

## See also

- [`examples/interleaver/gen/interleaver_canon.cpp`](../../../../examples/interleaver/gen/interleaver_canon.cpp)
- [Component structure](../../comp_codegen/structure.md)
