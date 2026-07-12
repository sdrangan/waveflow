---
title: Concurrency (Python) — SOB pattern
parent: Concurrency
nav_order: 2
audience: python
api: [StreamOfBlocksIF, SobIFMaster, SobIFSlave, acquire_write, acquire_read]
summary: "SOB modeling pattern in Python task networks: ping-pong block channels between fill/compute/store stages, anchored by the interleaver Fill→Gather-style flow."
---

# Concurrency (Python) — SOB pattern

Use SOB (`StreamOfBlocksIF`) when a stage needs random access over a resident block, not a pure FIFO.

## Fill → Gather-style pattern

The canonical shape in `examples/interleaver/interleaver.py` is:

- `il_load`: fill `p_blk` and `x_blk` from incoming word streams
- `il_compute`: read-lock both blocks and gather into `y_blk`
- `il_store`: read-lock `y_blk` and stream out words

This is modeled in Python with `SobIFMaster`/`SobIFSlave` and `acquire_*`/`commit`/`release` calls,
then lowered to `hls::stream_of_blocks<... ,2>` in HLS.

## Why SOB here

- block handoff keeps the random-access working set resident
- depth-2 ping-pong enables overlap between producer and consumer blocks
- lock scopes encode ownership boundaries per stage

## Boundaries

This page is about **modeling pattern**. Interface mechanics are in
[Stream-of-Blocks Interface](../../interface/sob.md), and HLS synthesis/performance implications are in
[Concurrency (HLS) — SOB](../hls/sob.md).
