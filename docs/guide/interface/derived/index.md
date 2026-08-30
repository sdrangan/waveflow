---
title: Derived interfaces
parent: Interfaces
nav_order: 3
has_children: true
audience: python
summary: "The interfaces that are transaction patterns over a primitive rather than a construct of their own — the two reverse channels (credit and ack), a schema transfer, an array transfer, and the AXI-MM command queue. A derived interface has no kind_of_endpoint kind of its own; the ones that lower do so by DECOMPOSING into the primitives they are built from, and the ones that do not decompose do not lower at all."
---

# Derived interfaces

A **derived** interface is a transaction pattern layered on a [primitive](../primitive/). It has no
`kind_of_endpoint` kind of its own, because there is no port kind to give it.

The source already says so. `derive_internal_edges` describes an `AckedStreamIF` as *"two FIFOs that
a module wants to talk about as one thing"*, which is the definition of derived: one name, one set of
methods, and underneath it the primitives that actually reach the boundary.

## Decomposing is what makes one lowerable

Being derived does not by itself say whether a design using the interface can be synthesized. What
decides that is whether the interface **decomposes** — whether `physical_endpoints()` and
`physical_interfaces()` hand the codegen walk the primitive channels underneath. And on that,
the five pages here split cleanly in two:

| | decomposes? | lowers? |
|---|---|---|
| [Credit Stream](./credit_stream.md) | yes — two `StreamIF`s | **yes**, as two ordinary streams |
| [Acked Stream](./acked_stream.md) | yes — two `StreamIF`s | **yes**, as two ordinary streams |
| [AXI-MM Command Queue](./mmqueue.md) | it *is* an `MMIFMaster` protocol | the port lowers; the ring is a hand-written hook |
| [Schema Transfer](./schema_transfer.md) | **no** | **no** — see the page |
| [Array Transfer](./array_transfer.md) | **no** | **no** — see the page |

The two reverse channels are the pattern worth copying: in C++ there is no credit-stream or
acked-stream object at all, just a pair of `hls::stream` plus a couple of registers, so nothing new
had to be shown to work.

## The two reverse channels

Both are a forward stream plus a second stream running the other way, and they are **not two
flavours of one mechanism**:

| | [Credit](./credit_stream.md) | [Acked](./acked_stream.md) |
|---|---|---|
| answers | *"May I send? Is there room?"* | *"What became of what I sent?"* |
| arrives | **before** the send | **after** the send |
| who could possibly know | the **channel** | only the **consumer** |
| carries | cumulative words consumed | one outcome per **marked** item |
| could a FIFO do this? | yes — `TREADY` *is* credit, one unit at a time at the moment of use | **no**: a dropped TX sample is a missed deadline, delivered perfectly and simply late |

They live in one module because the rules underneath them are genuinely shared — and so is the
masked-counter arithmetic, which is the one thing most likely to be got wrong in exactly one of two
copies.

## Pages

- [Schema Transfer Interface](./schema_transfer.md) — carrying serializable schema objects over a
  transport.
- [Array Transfer Interface](./array_transfer.md) — carrying a variable-length typed array over a
  transport.
- [AXI-MM Command Queue](./mmqueue.md) — the in-memory command ring (`AXIMMQueue`): control moved
  off the stream and into shared memory, over an `MMIFMaster`.
- [Credit Stream](./credit_stream.md) — the receiver's reverse channel: cumulative words consumed,
  so a producer that cannot be stalled can ask about room *before* it commits.
- [Acked Stream](./acked_stream.md) — the transmitter's reverse channel: one outcome per marked
  item, with positional token recovery and no id on the wire.
