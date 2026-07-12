---
title: Concurrency (Python) — LCS task network
parent: Concurrency
nav_order: 3
audience: python
api: [InterleaverCanon, StreamEdge, SobEdge, KernelTask, composite_top_spec]
summary: "Load-compute-store as a generated six-stage task network, anchored by the canonical interleaver topology and its forwarded per-job token pacing."
---

# Concurrency (Python) — LCS task network

This is the canonical Waveflow LCS task-network example: `InterleaverCanon`.

## As-built canonical topology

`cmd_rx → il_mem_r → il_load → il_compute → il_store → il_mem_w → s_done`

The graph has:

- 5 command/token `StreamEdge`s (`cmd0..cmd4`)
- 3 data `StreamEdge`s (`pwords`, `xwords`, `ywords`)
- 3 `SobEdge`s (`p_blk`, `x_blk`, `y_blk`)
- 2 `m_axi` bundles (`gmem0` read, `gmem1` write)

## Token forwarding and bounded in-flight depth

Each job carries one forwarded command token through all six stages. That pacing keeps each stage to
one in-flight job and avoids the prior `nj=8` deadlock class (`done == #tasks + 1`) documented in the
interleaver XSI/test comments.

## Throughput note (reconciled)

For the generated canonical interleaver, steady-state is **414 cycles/job**.  
The **295 cycles/job** figure belongs to the earlier hand-written sob3 reference, not this generated
canonical topology.

## Reusable vs bespoke

- reusable sub-component path: `MemRStream`/`MemWStream` composition (see [subcomponent](./subcomponent.md))
- bespoke canonical interleaver path: six stage-specific `KernelTask` tiles composed and lowered by
  `composite_top_spec`

## See also

- [`examples/interleaver/interleaver.py`](../../../../examples/interleaver/interleaver.py)
- [`tests/examples/test_interleaver_canon.py`](../../../../tests/examples/test_interleaver_canon.py)
- [`examples/interleaver/xsi/interleaver_canon_bfm_tb.cpp`](../../../../examples/interleaver/xsi/interleaver_canon_bfm_tb.cpp)
- [Custom Hooks: Dataflow](../../custom_hooks/dataflow.md) for the single-kernel LCS realization
