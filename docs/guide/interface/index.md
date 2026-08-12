---
title: Interfaces
parent: Guide
nav_order: 5
has_children: true
audience: python
summary: "The Python transactional interface model — how components communicate over streams, memory-mapped ports, register maps, and schema/array transfers in the SimPy simulation."
---

# Hardware Interfaces

Waveflow models hardware communication channels as **interfaces** — transactional connections between `SimObj` instances in a SimPy discrete-event simulation. Interfaces decouple the timing model of a bus from the functional logic of the components connected to it.

This section is the **Python transactional model**: the interface classes, their master/slave endpoints, the `write` / `read` / `get` calls, binding, and the cycle-based latency model. It is Python-only by design — an interface is not a standalone synthesizable artifact but a *port of a component*, so its synthesizable side is documented where the kernel is.

## Pages

- [Overview](./overview.md) — the core model: `Interface` vs endpoint, the `Words` type, latency, and the SimPy lifecycle.
- [Stream Interfaces](./stream.md) — unidirectional streams (`StreamIF`, `CrossBarIF`) and pipelined transfer.
- [Stream-of-Blocks Interface](./sob.md) — block handoff (`DataArray[T, N]`) over `write_lock` / `read_lock`.
- [MM Interfaces](./aximm.md) — memory-mapped read/write (`AXIMMCrossBarIF`, `DirectMMIF`).
- [Polling Overhead](./poll.md) — the loosely-timed polling model (`MMIFMaster.poll_until`): bandwidth steal + discovery latency.
- [AXI-MM Command Queue](./mmqueue.md) — the in-memory command ring (`AXIMMQueue`): control moved off the stream and into shared memory.
- [Register Maps](./regmap.md) — AXI-Lite control/status fields (`RegMap`, `VitisRegMap`).
- [Schema Transfer Interface](./schema_transfer.md) — carrying serializable schema objects over a transport.
- [Array Transfer Interface](./array_transfer.md) — carrying a variable-length typed array over a transport.
- [Behavioral Edges](./behavioral.md) — an interface that carries **behavior and state**, not only wiring: the `xsi_model()` hook, the `BlockChannel` primitive, and the phase discipline that makes a model-to-model transfer order-independent.

## See also

- [Hardware Modules](../flows/modules.md) — declaring the ports (these endpoints) on a component.
- [Custom Hooks](../custom_hooks/) — the synthesizable side: using an interface inside a kernel body (the `#pragma HLS INTERFACE` ports and the `read_stream` / `m_axi` calls on it).
