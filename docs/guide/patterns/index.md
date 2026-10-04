---
title: Design patterns
parent: Guide
nav_order: 1.5
has_children: true
summary: "How to structure an accelerator: the shapes accelerators in Waveflow take, before which layer each piece lives in. A catalogue of the system-level patterns -- command-response (a command carrying n, the work on n elements, a response; also through a pipeline, where the command travels with the data), the continuous stream, the configured stream, and the host-launched one-shot -- each with when to use it and the examples that use it. The rest of the guide is organized by layer; a pattern cuts across every layer, so its page tells the whole story once and links into them."
---

# Design patterns

The rest of this guide is organized by **layer** -- schemas, interfaces, code generation, hooks,
timing. A design is not: it has a *shape* first, and every layer is a part of that shape. This section
names the shapes the examples are built in, says when each is the right one, and for each tells the
whole story once, linking into the layer pages for detail.

## The catalogue

| pattern | shape | use it when | examples |
|---|---|---|---|
| [**Command–response**](./command_response.md) | a command carrying `n` → the work on `n` elements → a response | the work comes in discrete jobs. The easiest to verify and the most robust: every job is named, bounded and answered | [poly](../../examples/stream_inband/index.md), [vecmult](../../examples/vecmult/index.md), [mm_fir](../../examples/mm_fir/index.md); through a pipeline: [mem_copy](../../examples/memcpy/index.md), [interleaver](../../examples/interleaver/index.md), [markov](../../examples/markov/index.md) |
| **Continuous stream** | samples in → samples out, no commands | an always-on signal chain, where there is no "job" -- the data never stops | [rf_loopback](../../examples/rf_loopback/index.md) |
| **Configured stream** | parameters written once (registers), then a continuous stream | a stream whose parameters change rarely, and when they do the exact sample they take effect on does not matter | the RF data converter's settings; [mm_fir](../../examples/mm_fir/index.md)'s taps are the half-way case -- registers, but each packet names the config it needs |
| **Host-launched one-shot** | write the arguments, `ap_start`, wait for the interrupt, read the result | a classic Vitis kernel: one call, one result, under a host program | [regmap](../../examples/regmap/index.md) |

**Command–response is the default** in this repo, and the one to start from. Most hardware accelerators
in practice are some form of it -- DMA descriptors, NVMe and virtio queues, GPU command buffers, a Vitis
host launching a kernel -- because a job that is named, bounded and answered is a job you can check.
The other three are real and have their place; they are listed so a design that is not a sequence of
jobs is recognized as such, not forced into one.

Two choices sit *inside* every pattern and have pages of their own:

- **How the kernel body loops** -- the lane loop for packed samples, a straight-line loop per message
  versus a state machine: [Design patterns for loop optimization](../vectorization/hls/loop_optimization.md).
- **How a producer knows there is room** -- blocking on a stream, or credit when blocking would stall
  something shared: [Credit stream](../interface/derived/credit_stream.md).
