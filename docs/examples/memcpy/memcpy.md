---
title: Module Overview
parent: Memory Copy
nav_order: 1
---
# Memory Copy

`mem_copy` copies a run of words from one memory region to another. It is deliberately the simplest
design that still exercises the whole [concurrent (free-running) flow](../../guide/flows/concurrent.md):
a **composite of free-running `hls::task`s** with two `m_axi` memory ports, internal streams between the
stages, and a completion signal back to the host. Nothing is computed — the payload is copied unchanged
— so the design's *structure* is the subject, not its math.

## Why a data mover is worth building

Even though it looks like a toy, a **data mover** is a genuinely useful hardware block. Moving a buffer
from one place in memory to another — or between memory and a peripheral — is work a CPU can do, but
doing it word-by-word burns cycles the CPU could spend on real computation. Offloading that copy to a
small piece of fabric frees the host: it issues one command and the accelerator performs the whole burst
transfer on its own. This is exactly what a **DMA engine** does in an SoC, and `mem_copy` is a minimal
version of that pattern — which is why it is also a natural first free-running example.

## The three-stage structure

The design (`examples/mem_copy/`) is a three-stage pipeline, driven by a command stream and reporting on
a done stream. Each stage is a **leaf `FreeRunMod`** — it implements `run_iter` (one firing per job)
and lowers to one `ap_ctrl_none` `hls::task`:

- **`Sequencer`** — a pure-stream stage that owns no memory. It reads one `CopyCmd` from the boundary
  stream `s_cmd` and **frames** it into one stream carrying `[MemRCmd | MemWCmd | CopyResp]`: a read
  descriptor, a write descriptor, and the typed response. It is the **only schema-aware stage**.
- **`MemRStream`** — the `m_axi` **read** owner. It reads the `MemRCmd`, relays the opaque bursts that
  follow it (the `MemWCmd` + the `CopyResp`), then bursts the source region from its memory port
  (`m_in`, bundle `gmem0`) and **appends** it — emitting `[MemWCmd | CopyResp | data]`.
- **`MemWStream`** — the `m_axi` **write** owner. It reads the `MemWCmd`, buffers the `CopyResp` across
  the write, drains the data into the destination region (`m_out`, bundle `gmem1`), then emits the
  buffered `CopyResp` on the boundary stream `s_done` — echoing the request's `tx_id`.

## The forwarding chain

Everything rides **one framed stream, in forwarding order**, and each stage **strips the one descriptor
addressed to it and relays the rest opaquely** — a symmetric onion. `MemRCmd.fwd_bursts=2` tells the
reader to relay the next two bursts (the `MemWCmd` + `CopyResp`); `MemWCmd.fwd_bursts=1` tells the writer
to relay the `CopyResp`. Because a descriptor and its data are contiguous on one stream, a command can
never pair with the wrong data — and because a relaying stage never parses what it forwards, the
mem-streams stay application-agnostic.

```mermaid
flowchart LR
  cmd([CopyCmd]) --> SEQ[Sequencer]
  SEQ -->|"MemRCmd · MemWCmd · CopyResp"| MR[MemRStream]
  MR -->|"MemWCmd · CopyResp · data"| MW[MemWStream]
  MW --> resp([CopyResp])
  m_in[("m_in · gmem0")] -. read .-> MR
  MW -. write .-> m_out[("m_out · gmem1")]
```

Only **two** internal edges carry this — `cmd` (Sequencer → MemRStream) and `copy_data` (MemRStream →
MemWStream); both are `framed_word` FIFOs inside the generated top and never appear at the boundary.
What *does* appear at the boundary is four ports: the command in (`s_cmd`), the two `m_axi` masters
(`m_in`/`gmem0`, `m_out`/`gmem1`), and the completion out (`s_done`).

Because the stages are free-running, they **overlap**: while `MemWStream` is still storing job *j*, the
`Sequencer` and `MemRStream` can already be working on job *j+1*. That pipelining is the whole reason the
stages are `ap_ctrl_none` tasks rather than host-launched — it is what makes the per-job cost
`max(read, write)` (plus the small in-band descriptor beats) instead of `read + write`.

## Building blocks

`MemRStream` and `MemWStream` are **framework** components (`waveflow/hw/mem_stream.py`), not part of
this example — any accelerator can compose them as its load / store stage. They are documented in
[Streaming Memory Kernels](../../guide/memory/memstream.md), which also walks the generated composite top
and the generated XSI testbench in detail. Only the `Sequencer` and the composite wiring are specific to
`mem_copy`; the following pages build the example up from there.

**Source:** [`examples/mem_copy/`](../../../examples/mem_copy/).
