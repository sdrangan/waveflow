---
title: Sub-component to hls::task
parent: HLS
grand_parent: Concurrency
nav_order: 2
audience: hls
api: [KernelTask, composite_top_spec, hls::task]
summary: "How hierarchical sub-components lower to `hls::task` calls via `KernelTask` signatures and graph-derived argument resolution."
---

# Concurrency (HLS) — Sub-component to `hls::task`

Each active sub-component in the composite exposes a `KernelTask` descriptor:

- task function name/header
- endpoint signature order
- template arguments

`composite_top_spec` resolves each endpoint in that signature to either:

- a top boundary port, or
- an internal edge channel

and emits one `hls::task` instantiation per sub-component in graph order.

## Ownership boundary rule

Memory-owner tasks should keep ownership boundaries clean:

- `m_axi` owners: streams/tokens + memory
- SOB owners: lock-scoped block handoff

Do not mix incompatible resource ownership in one task unless a specific design proves safe.

## See also

- [`waveflow/build/composite_gen.py`](../../../../waveflow/build/composite_gen.py)
- [`waveflow/hw/mem_stream.py`](../../../../waveflow/hw/mem_stream.py)
