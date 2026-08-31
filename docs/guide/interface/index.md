---
title: Interfaces
parent: Guide
nav_order: 5
has_children: true
audience: python
snippets: run
summary: "The Python transactional interface model — how modules communicate over streams, memory-mapped ports, BRAM, register maps, and schema/array transfers in the SimPy simulation. Presented in two parts, split by one question: does this interface build on another one? A primitive is a module’s direct connection to the outside; a derived one composes primitives whose endpoints it owns and drives."
---

# Hardware Interfaces

Waveflow models hardware communication channels as **interfaces** — transactional connections between `SimObj` instances in a SimPy discrete-event simulation. Interfaces decouple the timing model of a bus from the functional logic of the modules connected to it.

This section is the **Python transactional model**: the interface classes, their master/slave endpoints, the `write` / `read` / `get` calls, binding, and the cycle-based latency model. It is Python-only by design — an interface is not a standalone synthesizable artifact but a *port of a module*, so its synthesizable side is documented where the kernel is, in [Module Code Generation](../comp_codegen/) and [Custom Hooks](../custom_hooks/).

Start with [Overview](./overview.md) for the core model — `Interface` versus endpoint, the `Words` type, the latency model, and the SimPy lifecycle — then pick an interface from the map below.

## Interfaces compose, and that is the map

A key property of Waveflow interfaces is that they are **composable**: an interface endpoint may use
the methods of *other* endpoints to build a richer transactional behaviour on top of them. An
`AckedStreamIF` is not a new kind of wire — it is two ordinary streams that a module wants to talk
about as one thing, and it says so:

```python
from waveflow.hw.reverse_stream import AckedStreamIF
print(AckedStreamIF.physical_interfaces.__doc__.splitlines()[0])
```

```text
Two ordinary streams.  In hardware there is no acked stream — there are two FIFOs.
```

So the guide is in two parts, and the test is one question — **does this interface build on another
one?**

| | | |
|---|---|---|
| **[Primitive](./primitive/)** | builds on no other interface — it is the module's direct connection to the outside | [`StreamIF`](./primitive/stream.md) · [`MMIF` / `DirectMMIF`](./primitive/aximm.md) · [`BramIF`](./primitive/bram.md) · [`RegMapMMIFSlave`](./primitive/regmap.md) · [`StreamOfBlocksIF`](./primitive/sob.md) · [`CrossBarIF`](./primitive/crossbar.md) · [`RFSampIF`](../rf/rfdc/) |
| **[Derived](./derived/)** | built from one or more primitives, whose endpoints it owns and drives | [`CreditStreamIF`](./derived/credit_stream.md) · [`AckedStreamIF`](./derived/acked_stream.md) · [`AXIMMQueue`](./derived/mmqueue.md) |

Primitives come first, because everything in the second row is written in terms of the first.

Two of them are worth pointing at directly. [`RFSampIF`](../rf/rfdc/) is primitive — it composes
nothing — but it is domain-specific, so it lives with its domain rather than here.
[`StreamOfBlocksIF`](./primitive/sob.md) is primitive too, though it only ever appears *inside* a
module rather than on its boundary.

> **A forward reference, not a definition.** When a module is *lowered* to an HLS or XSI target, a
> primitive endpoint typically becomes a port on the generated kernel, and a derived one becomes
> whatever its underlying primitives become — nothing new appears in the hardware. How that
> happens, and what the machinery is called, is
> [Endpoint kinds](../comp_codegen/endpoint_kinds.md). **You do not need any of it to use an
> interface**, which is why it is not here.

