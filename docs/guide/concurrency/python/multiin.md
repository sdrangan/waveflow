---
title: Concurrency (Python) — Multi-input stages
parent: Concurrency
nav_order: 4
audience: python
api: [IlLoad, IlCompute, StreamIF, StreamOfBlocksIF]
summary: "How canonical interleaver stages consume multiple inputs: `il_load` (two stream inputs to two SOB outputs) and `il_compute` (two SOB read-lock inputs). Includes arbitration status."
---

# Concurrency (Python) — Multi-input stages

The canonical interleaver already has multi-input stages:

- `il_load`: consumes `pwords` + `xwords`, then fills two SOB blocks (`p_blk`, `x_blk`)
- `il_compute`: acquires two SOB read locks (`p_blk`, `x_blk`) and writes one SOB output (`y_blk`)

`il_mem_r` is also dual-output by construction (`pwords` and `xwords`); there is no separate Demux
stage in the as-built canonical topology.

## Deferred area: multi-master arbitration

> **Deferred / not implemented as a general feature:** multi-master arbitration policies are not
> presented as implemented in this path. The current canonical interleaver uses fixed ownership
> boundaries (`gmem0` read owner, `gmem1` write owner) and explicit stage wiring.

## See also

- [LCS task network](./lcs.md)
- [SOB pattern](./sob.md)
- [`waveflow/build/il_mem_r_task.h`](../../../../waveflow/build/il_mem_r_task.h)
