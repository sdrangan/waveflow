---
title: Derived interfaces
parent: Interfaces
nav_order: 3
has_children: true
audience: python
summary: "The interfaces that are transaction patterns over a primitive rather than a construct of their own — a schema transfer, an array transfer, and the AXI-MM command queue. A derived interface has no kind_of_endpoint kind: it lowers by lowering the primitive underneath it."
---

# Derived interfaces

A **derived** interface is a transaction pattern layered on a
[primitive](../primitive/). It has no `kind_of_endpoint` kind of its own, because there is no port
kind to give it — it lowers by lowering the primitive underneath.

The source already says so. `derive_internal_edges` describes an `AckedStreamIF` as *"two FIFOs
that a module wants to talk about as one thing"*, which is the definition of derived: one name, one
set of methods, and underneath it the primitives that actually reach the boundary.

## Pages

- [Schema Transfer Interface](./schema_transfer.md) — carrying serializable schema objects over a
  transport.
- [Array Transfer Interface](./array_transfer.md) — carrying a variable-length typed array over a
  transport.
- [AXI-MM Command Queue](./mmqueue.md) — the in-memory command ring (`AXIMMQueue`): control moved
  off the stream and into shared memory, over an `MMIFMaster`.

`CreditStreamIF` and `AckedStreamIF` (`waveflow/hw/reverse_stream.py`) belong on this list and are
not yet documented.
