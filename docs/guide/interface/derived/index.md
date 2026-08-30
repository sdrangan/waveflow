---
title: Derived interfaces
parent: Interfaces
nav_order: 3
has_children: true
audience: python
snippets: run
summary: "The interfaces that are transaction patterns over a primitive rather than a construct of their own — the two reverse channels (credit and ack), a schema transfer, an array transfer, and the AXI-MM command queue. Each is built from primitives; what differs is how it hands you the primitive underneath — declared and wired automatically, or owned and bound by you."
---

# Derived interfaces

A **derived** interface is a transaction pattern layered on a [primitive](../primitive/). It has no
`kind_of_endpoint` kind of its own, because there is no port kind to give it.

The source already says so. `derive_internal_edges` describes an `AckedStreamIF` as *"two FIFOs that
a module wants to talk about as one thing"*, which is the definition of derived: one name, one set of
methods, and underneath it the primitives that actually reach the boundary.

## How a derived interface decomposes

Every interface here is built from primitives, but they differ in **how they hand you the primitive
underneath** — and that difference is worth knowing before you wire one up.

| | how it composes |
|---|---|
| [Credit Stream](./credit_stream.md) | **declares** two `StreamIF`s and exposes them; wiring is automatic |
| [Acked Stream](./acked_stream.md) | **declares** two `StreamIF`s and exposes them; wiring is automatic |
| [AXI-MM Command Queue](./mmqueue.md) | a protocol *over* an `MMIFMaster` — the ring lives in the transactions, not in a new channel |
| [Schema Transfer](./schema_transfer.md) | **owns** an inner `StreamIFMaster`; **you bind its `stream_ep`** to a real `StreamIF` |
| [Array Transfer](./array_transfer.md) | **owns** an inner stream endpoint, bound the same way |

The two reverse channels declare their composition, so a walk over the design finds the underlying
streams without help:

```python
from waveflow.hw.reverse_stream import CreditStreamIF, AckedStreamIF
for cls in (CreditStreamIF, AckedStreamIF):
    print(f"{cls.__name__:16s} {cls.physical_interfaces.__doc__.splitlines()[0]}")
```

```text
CreditStreamIF   Two ordinary streams.  Nothing here lowers to a new kind of edge.
AckedStreamIF    Two ordinary streams.  In hardware there is no acked stream — there are two FIFOs.
```

The two transfer interfaces compose just as really, but the seam is manual: the endpoint creates the
inner `StreamIFMaster` and you complete the connection yourself. Nothing is lost by that — the
stream you bind is an ordinary one — but it does mean the composition is not visible to a walk that
only reads the interface.

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
