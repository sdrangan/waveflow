---
title: Concurrency (HLS) — SOB synthesis notes
parent: Concurrency
nav_order: 8
audience: hls
api: [hls::stream_of_blocks, hls::write_lock, hls::read_lock]
summary: "SOB in task-network synthesis: depth-2 block channels with lock scopes, DTLP-oriented ownership split, and gather/scatter throughput notes."
---

# Concurrency (HLS) — SOB synthesis notes

In the canonical interleaver, SOB edges lower to:

`hls::stream_of_blocks<ap_uint<MEM_DW>[NW], 2>`

with `hls::write_lock` / `hls::read_lock` in the stage bodies.

## DTLP-oriented split

The interleaver keeps `m_axi` tasks separate from SOB-lock tasks:

- memory tasks (`il_mem_r`, `il_mem_w`) do stream + memory work
- block tasks (`il_load`, `il_compute`, `il_store`) do lock-scoped SOB work

This fill/gather/store split avoids mixing `m_axi` and SOB lock ownership in one task.

## Throughput asymmetry note

Current SOB consumer guidance in `SobIFSlave` records:

- gather (random reads): faster effective access path
- scatter (random writes): serialized by write hazards

So gather/scatter are not treated as symmetric throughput paths.

## See also

- [`waveflow/hw/interface.py`](../../../../waveflow/hw/interface.py)
- [`waveflow/build/il_load_task.h`](../../../../waveflow/build/il_load_task.h)
- [`waveflow/build/il_compute_task.h`](../../../../waveflow/build/il_compute_task.h)
