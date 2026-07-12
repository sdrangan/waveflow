---
title: Stream-of-Blocks Interface
parent: Interfaces
nav_order: 2.5
audience: python
api: [StreamOfBlocksIF, SobIFMaster, SobIFSlave, DataArray]
summary: "What Stream-of-Blocks (SOB) is in isolation: block-granular handoff (`DataArray[T, N]`) with `write_lock` / `read_lock` acquire-release semantics over a depth-2 ping-pong buffer."
---

# Stream-of-Blocks Interface

`StreamOfBlocksIF` is the block counterpart to `StreamIF`: instead of transferring one word at a
time, it transfers ownership of a whole block.

## What it carries

SOB transfers a fixed-size block whose element shape is:

`DataArray[T, N]`

- `T` is the element type (`ap_uint<MEM_DW>` in the interleaver path).
- `N` is the block length (`block_n`).

The producer and consumer endpoints are:

- `SobIFMaster` (producer side)
- `SobIFSlave` (consumer side)

## Acquire / release contract

SOB is a lock-scoped handoff:

- producer acquires a free block (`acquire_write`) → fills it → commits (`commit_write`)
- consumer acquires a committed block (`acquire_read`) → random-accesses it → releases (`release_read`)

In HLS lowering this is the `write_lock` / `read_lock` pattern over
`hls::stream_of_blocks<T[N], 2>`.

## Lowering snapshot

| Interface kind | Python model | HLS lowering |
|---|---|---|
| Stream | `StreamIFMaster` / `StreamIFSlave` | `hls::stream<...>` (`axis`) |
| Memory-mapped | `MMIFMaster` | `m_axi` pointer |
| Stream-of-Blocks | `SobIFMaster` / `SobIFSlave` on `StreamOfBlocksIF` | `hls::stream_of_blocks<T[N], 2>` with `write_lock` / `read_lock` |

`StreamOfBlocksIF` is for internal task-network channels, not a host boundary transport.

## Ping-pong behavior in simulation

The Python model uses a free-buffer counter plus ready queue (depth defaults to 2), so one block can
be filled while the previous block is still held by the consumer.

## See also

- [Stream Interfaces](./stream.md) — FIFO stream semantics.
- [Defining a component](../components/overview.md) — where SOB endpoints are declared on components.
- [Concurrency](../concurrency/) — composition/performance context for SOB in task networks.
