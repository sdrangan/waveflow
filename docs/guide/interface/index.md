---
title: Interfaces
parent: Guide
nav_order: 5
has_children: true
audience: python
summary: "The Python transactional interface model — how modules communicate over streams, memory-mapped ports, BRAM, register maps, and schema/array transfers in the SimPy simulation. Presented in tiers rather than as a flat list: a primitive interface lowers to a real HLS construct, a derived one is a transaction pattern over a primitive, and a simulation-only one does not lower at all."
---

# Hardware Interfaces

Waveflow models hardware communication channels as **interfaces** — transactional connections between `SimObj` instances in a SimPy discrete-event simulation. Interfaces decouple the timing model of a bus from the functional logic of the modules connected to it.

This section is the **Python transactional model**: the interface classes, their master/slave endpoints, the `write` / `read` / `get` calls, binding, and the cycle-based latency model. It is Python-only by design — an interface is not a standalone synthesizable artifact but a *port of a module*, so its synthesizable side is documented where the kernel is, in [Module Code Generation](../comp_codegen/) and [Custom Hooks](../custom_hooks/).

Start with [Overview](./overview.md) for the core model — `Interface` versus endpoint, the `Words` type, the latency model, and the SimPy lifecycle — then pick an interface from the map below.

## The map

The test for which tier an interface belongs to is not a matter of taste: it is whether the
interface has a `kind_of_endpoint` kind, and if not, what it lowers to instead.

| Tier | Test | Interfaces |
|---|---|---|
| **[Primitive, boundary](./primitive/)** | has a `kind_of_endpoint` kind — it becomes a port on the generated kernel | [`StreamIF`](./primitive/stream.md) · [`MMIF` / `DirectMMIF`](./primitive/aximm.md) · [`BramIF`](./primitive/bram.md) · [`RegMapMMIFSlave`](./primitive/regmap.md) |
| **[Primitive, internal](./primitive/)** | lowers to a real HLS construct, but never to a boundary port | [`StreamOfBlocksIF`](./primitive/sob.md) → `hls::stream_of_blocks` · [`CrossBarIF`](./primitive/crossbar.md) → the n × m fabric |
| **[Derived](./derived/)** | a transaction pattern over a primitive; no kind of its own | [`SchemaTransferIF`](./derived/schema_transfer.md) · [`ArrayTransferIF`](./derived/array_transfer.md) · [`AXIMMQueue`](./derived/mmqueue.md) · `CreditStreamIF` · `AckedStreamIF` |
| **Simulation-only** | no lowering at all | [`RFSampIF`](../rf/rfdc/) — a domain-specific interface, documented with its domain |

**Why the middle tier earns its place.** Without it, `StreamOfBlocksIF` looks derived — it has no
boundary kind — when it is really a primitive that only exists *inside* a kernel. Boundary versus
internal is a column here and in [Primitive interfaces](./primitive/), not a third folder: it
changes how a page reads about lowering and nothing else.

`CreditStreamIF` and `AckedStreamIF` (`waveflow/hw/reverse_stream.py`) are listed because they are
derived interfaces that exist in the tree; their pages are not yet written.

## Not interfaces

Two things that used to be filed here, and where they went:

- **[Polling overhead](../timing_model/poll.md)** — `MMIFMaster.poll_until`, the bandwidth-steal
  derating and the discovery-latency delay. A loosely-timed **timing model** reached through an
  interface, so it lives with the other timing models.
- **[Behavioral edges](../custom_hooks/behavioral.md)** — the `xsi_model()` hook and the
  `BlockChannel` primitive. An **authoring guide**, so it sits beside
  [`bfm_model()`](../custom_hooks/bfm_model.md).

## See also

- [Hardware Modules](../flows/modules.md) — declaring the ports (these endpoints) on a module.
- [Interface lowering](../comp_codegen/interface.md) — the boundary-port emitter and the
  `kind_of_endpoint` vocabulary this page's tier test is built on.
- [Custom Hooks](../custom_hooks/) — using an interface inside a hand-written kernel body.
