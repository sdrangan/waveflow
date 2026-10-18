---
title: Stream Blocks
parent: Guide
nav_order: 10.6
has_children: true
audience: python
summary: "Waveflow's own stream blocks: small, general free-running modules that move framed streams between a design's kernels, so a stream-only design does not rewrite them. Each is framework code -- a Python model, an HLS body templated on its ports' word types (so one body serves the block at a top, at a composite's boundary and inside one), a build step that copies the body, and an RTL gate in its own harness. Today: StreamBroadcast, one stream in and n copies out."
---

# Stream Blocks

A stream-only design -- kernels that read and write only streams, joined by streams
([Stream-only modules and adaptors](../patterns/stream_only.md)) -- keeps needing the same few pieces
between its kernels: one stream to several stages, several streams into one. These are those pieces,
written once, verified once, in `waveflow.streams`.

Every block here follows the same shape:

- **a Python model** -- a `FreeRunMod` whose `run_iter` moves whole bursts, so its framing in pysim is the
  RTL's;
- **an HLS body** in `waveflow/streams/src/`, templated on the data width and on each port's **word type**.
  The generator supplies the type where the block sits (`KernelTask.framed`): `axi4s_word<W>` on a top's
  port, `framed_word<W>` on an internal FIFO. So one body serves the block as the top of its own harness,
  at a composite's boundary, and deep inside one;
- **a build step** that copies the body into a design's `include/`;
- **an RTL gate in its own harness** (`waveflow.streams.rtl`): every output recorded at RTL equal to
  pysim's.

| block | what it does |
|---|---|
| [`StreamBroadcast`](broadcast.md) | one framed stream in, `n_out` copies out: every word to every output, TLAST included |
