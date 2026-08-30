---
title: Primitive interfaces
parent: Interfaces
nav_order: 2
has_children: true
audience: python
summary: "The interfaces that have a real HLS lowering — a stream, a memory-mapped port, a BRAM port, an AXI-Lite register map, a stream-of-blocks, a crossbar. Split by position: a boundary primitive becomes a port on the generated kernel and has a kind_of_endpoint kind; an internal primitive lowers to an HLS construct that only exists inside the kernel."
---

# Primitive interfaces

A **primitive** interface is one that lowers to a real HLS construct. It is not built out of
anything else in this section — it is the bottom of the stack, and everything in
[Derived interfaces](../derived/) is a transaction pattern layered on top of one of these.

Primitives divide by **position**, which is a column rather than a folder because the same reader
needs both in one list:

| | test | pages |
|---|---|---|
| **Boundary** | becomes a port on the generated kernel, and has a `kind_of_endpoint` kind | [Stream](./stream.md) · [MM](./aximm.md) · [BRAM](./bram.md) · [Register map](./regmap.md) |
| **Internal** | lowers to an HLS construct that exists only *inside* the kernel | [Stream-of-blocks](./sob.md) · [Crossbar](./crossbar.md) |

The distinction matters when reading about lowering and nowhere else: a `StreamIFSlave` at a
boundary is an `axis_in` port, and the same endpoint on an internal edge is an `hls::stream` FIFO.
See [Interfaces](../) for the tier table this table refines, and
[Interface lowering](../../comp_codegen/interface.md) for the boundary-port emitter.

## Pages

- [Stream Interfaces](./stream.md) — unidirectional streams (`StreamIF`) and pipelined transfer.
- [MM Interfaces](./aximm.md) — memory-mapped read/write (`AXIMMCrossBarIF`, `DirectMMIF`).
- [BRAM — memory between modules](./bram.md) — `BramIF`: an on-chip memory shared by two tasks,
  which cannot live *inside* a Vitis kernel and so lives beside it as hand-written Verilog.
- [Register Maps](./regmap.md) — AXI-Lite control/status fields (`RegMap`, `RegField`, `RegAccess`).
- [Stream-of-Blocks Interface](./sob.md) — block handoff (`DataArray[T, N]`) over
  `write_lock` / `read_lock`. **Internal.**
- [Crossbar Interfaces](./crossbar.md) — the port-indexed n × m stream fabric (`CrossBarIF`).
  **Internal.**
