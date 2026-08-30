---
title: AXI-MM Command Queue
parent: Derived interfaces
grand_parent: Interfaces
nav_order: 3
audience: python
api: [AXIMMQueue, AXIMMQueueLayout, MMIFMaster, AXIMMCrossBarIF, DirectMMIF]
summary: "The in-memory command queue — AXIMMQueue, a host-driven ring buffer over an MMIFMaster (m_axi). The AXIMMQueueLayout (head/tail/capacity + data slots), the producer write / consumer get operations, and the loosely-timed poll-on-empty / backpressure-on-full timing model."
---

# AXI-MM Command Queue

The other interface pages move *data* off the control plane (memory-mapped reads
in [MM Interfaces](../primitive/aximm.md)) or carry control *in band* on a stream. The
**command queue** moves the control plane itself **into memory**: instead of the
host pushing each command down a dedicated stream, it appends commands to a ring
buffer in shared DRAM, and the accelerator dequeues them over its ordinary `m_axi`
master. Control and data now share one memory bundle.

This is the Python transactional model of that ring. `AXIMMQueue`
([`waveflow/hw/aximm_queue.py`](../../../../waveflow/hw/aximm_queue.py)) is **not a new
endpoint** — it is a thin proxy over an existing
[`MMIFMaster`](../primitive/aximm.md): the producer and the consumer each bind one to the same
region of memory and talk through `write` / `get`. The synthesizable side (the
consumer's `get` lowered to a hand-written ring-dequeue kernel) is documented in
[Custom Hooks → Memory command queue](../../custom_hooks/queue.md).

## What the queue is

A single-producer / single-consumer **ring buffer** laid out in memory by
`AXIMMQueueLayout`. The first four words are control; the slots follow:

```
 word 0   head        consumer-owned read index
 word 1   tail        producer-owned write index
 word 2   capacity    number of slots (informational)
 word 3   reserved    (room for status/flags)
 ─────────────────────────────────────────────
 data     capacity × elem_words   the command slots
```

`AXIMMQueueLayout(base_addr, capacity, elem_words, mem_bw)` computes the byte
addresses — `head_addr`, `tail_addr`, `capacity_addr`, `data_base`, and
`slot_addr(idx)` — from the base address and the memory word width. The ring is
**empty** when `head == tail` and **full** when `(tail + 1) % capacity == head`, so
the usable depth is `capacity - 1` (one slot distinguishes full from empty).

Contrast this with the stream command path of the
[shared-memory histogram](../../../examples/shared_mem/) example, where the command
rides a dedicated AXI4-Stream: there the control channel is a separate port; here it
is just another region of the same memory, and ordering between producer and
consumer is mediated entirely by the two pointers.

## Operations

A queue is created from a bound master and a layout:

```python
queue = AXIMMQueue(master=mm_master, layout=AXIMMQueueLayout(base, cap, elem_words, mem_bw))
```

- **`reset()`** — zero `head` and `tail`, record `capacity` in word 2. Called once
  by whichever side owns setup.
- **`write(data, element_type=None)`** *(producer / host)* — enqueue, blocking until
  the whole batch is in. With `element_type` given, `data` is serialized through the
  schema; with `element_type=None`, `data` is a raw word array. It advances `tail`
  by the number of slots written.
- **`get(schema_type=None, count=None)`** *(consumer)* — dequeue. The
  **typed single-element** form, `get(schema_type)`, reads one slot, deserializes it
  into one schema instance, and advances `head`. This is the form the accelerator
  uses in its run loop, and the one that lowers to hardware.

Pointer advance wraps modulo capacity — `head = (head + 1) % capacity`. When
`capacity` is a power of two (the synthesizable requirement), that modulo is a
bit-mask `& (capacity - 1)`, which is why the layout enforces a power-of-two
capacity on the hardware path.

A free-running consumer is just a loop over `get`:

```python
while True:
    cmd = yield from queue.get(Cmd)   # one command, deserialized
    if cmd.op == OpCode.end:          # sentinel ends the stream
        return
    ...                               # act on cmd
```

## Timing model

The queue inherits the **loosely-timed** model of its underlying `m_axi` master
(see [MM Interfaces](../primitive/aximm.md) and [Polling Overhead](../../timing_model/poll.md)). A `get`
reads `(head, tail)` and, when non-empty, one data slot — each a timed transaction
on the shared bus.

Two cases need the polling model:

- **Empty ring (`head == tail`).** The consumer must wait for the producer to
  advance `tail`. Rather than step every cycle, it uses the
  [`poll_until`](../../timing_model/poll.md) loosely-timed model: a bandwidth-steal (`ov`) derating
  on the shared bus plus a deterministic discovery-latency delay. The default poll
  interval is configurable per queue (`poll_interval`, in cycles).
- **Full ring.** The producer's `write` blocks (host backpressure) until the
  consumer frees a slot by advancing `head`.

Because the pointers and the data live in the same memory, occupancy and bus
contention both fall out of the standard interconnect model — the queue adds no new
timing primitive, only a new *use* of `m_axi` plus `poll_until`.

## Vehicles

- [`examples/interface/aximm_queue_demo.py`](../../../../examples/interface/aximm_queue_demo.py)
  — the minimal producer/consumer demo over one `AXIMMCrossBarIF`.
- The [VMAC example](../../../examples/mmqueue/) — the worked use: a host enqueues
  `VmacCmd`s and a free-running accelerator dequeues and executes them.

## See also

- [MM Interfaces](../primitive/aximm.md) — the `MMIFMaster` the queue is built on.
- [Polling Overhead](../../timing_model/poll.md) — the `poll_until` model the empty-ring wait uses.
- [Custom Hooks → Memory command queue](../../custom_hooks/queue.md) — the
  synthesizable side: `AXIMMQueue.get` lowered to the ring-dequeue kernel hook.
