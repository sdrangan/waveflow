---
title: Memory command queue
parent: Custom Hooks
nav_order: 5
audience: hls
api: [AXIMMQueue, AXIMMQueueGetStmt, queue_get, synthesizable]
summary: "The synthesizable side of the AXI-MM command queue: AXIMMQueue.get lowers to AXIMMQueueGetStmt, emitted as a call to the hand-written ring-dequeue hook queue_get<CmdT, MEM_BW, BASE, CAP, EW> in aximm_queue_impl.tpp — read (head,tail), poll while empty, read one slot, advance head with a power-of-two mask, deserialize the typed command."
---

# Memory command queue — the ring-dequeue hook

This is the **advanced case**: a hook that is not a datapath at all but the
**synthesizable half of a transport interface**. The
[AXI-MM command queue](../interface/mmqueue.md) has a Python transactional model
(`AXIMMQueue.write` / `get`); its synthesizable side is a hand-written ring-dequeue
hook that *is* what `get` lowers to inside a kernel. Where the
[block](./block.md) / [stream](./stream.md) / [complex](./complex.md) patterns are
hooks you write for *your* datapath, this one implements a reusable **interface**
once, and every kernel that dequeues a command gets it for free.

This page is that hook —
[`waveflow/build/aximm_queue_impl.tpp`](../../../waveflow/build/aximm_queue_impl.tpp)'s
`queue_get` — and how the extractor lowers a `get` call onto it. It is **merged and
Vitis-verified**: the [VMAC generated top](../../examples/mmqueue/codegen.md) calls it
and cosims bit-exact.

It is a stmt-class hook. The general `.tpp` contract (the bit-exact Python sibling,
the namespace, `#pragma HLS INLINE`) is [Writing a hook](./writing.md); the complex
datapath that consumes the dequeued command is [complex.md](./complex.md). This page
is the queue-specific dequeue.

## What lowers to it

In a synthesizable run loop, the consumer reads one typed command per iteration:

```python
cmd = yield from self.cmd_queue.get(self.Cmd)   # self.cmd_queue: AXIMMQueue
```

`AXIMMQueue.get` is decorated `@synthesizable(stmt_class=AXIMMQueueGetStmt)`. The
single-element typed form (`get(schema_type)`) is the synthesizable one: the extractor
emits an **`AXIMMQueueGetStmt`** — an IR node (a `SynthCallStmt`) whose `inputs[0]` is
the element schema class and whose `outputs[0]` is the bound command variable. This is
the exact parallel of `StreamGetStmt` (a stream `get`) and the register-map `get`
stmt; the queue is just a different transport.

Codegen lowers that stmt in
[`waveflow/build/hwgen.py`](../../../waveflow/build/hwgen.py) (`_emit_aximm_queue_get`)
to a declaration of the command struct plus a **call** to the `queue_get` hook,
passing the layout constants from the queue's `AXIMMQueueLayout` as template
arguments. So the generated top contains no hand-rolled ring code — only a call into
this hook.

## The hook contract

```cpp
template <typename CmdT, int MEM_BW, int BASE, int CAP, int EW>
void queue_get(ap_uint<MEM_BW>* gmem, CmdT& out);
```

| param | meaning |
| --- | --- |
| `CmdT` | the generated command struct (e.g. `VmacCmd`) |
| `MEM_BW` | memory word width in bits (the `gmem` word) |
| `BASE` | ring base **byte** address (word-aligned) |
| `CAP` | ring capacity in slots (**power of two**) |
| `EW` | words per slot (`== CmdT.nwords_per_inst(MEM_BW)`) |

Two `static_assert`s enforce the synthesis preconditions: `CAP` is a power of two
(`CAP >= 2 && (CAP & (CAP - 1)) == 0`) and `BASE` is word-aligned. Both come for free
from `AXIMMQueueLayout`.

## What it does

The hook is the single-producer / single-consumer dequeue, in five steps:

1. **Read `head` once.** The consumer owns `head`, so it is stable for the call — one
   `m_axi` read.
2. **Poll until non-empty.** While `tail == head` the ring is empty; the hook waits
   with `poll_until_ne(tail_word, head)` — the synthesizable analogue of the Python
   [`poll_until`](../interface/poll.md) wait — re-reading `tail` until the producer
   advances it.
3. **Read one slot.** Read `EW` words at `slot(head)` **before** advancing the pointer
   (SPSC ordering: consume the data, then publish that the slot is free).
4. **Advance `head`.** `head = (head + 1) & (CAP - 1)` — the `% capacity` wrap as a
   bit-mask, which is exactly why `CAP` must be a power of two — and write it back so
   the producer sees the freed slot.
5. **Deserialize.** Turn the slot's raw words into the typed command with the
   generated `out.read_array<MEM_BW>(slot)`, yielding the populated `CmdT& out`.

The command never crosses an `s_axilite` boundary — it is read straight from memory
and deserialized into a struct of scalars inside the kernel. That is what lets the
[generated top be `m_axi`-only](../../examples/mmqueue/codegen.md#why-the-top-has-only-maxi--apctrl)
and sidestep the nested-struct csynth pitfall.

## See also

- [AXI-MM Command Queue](../interface/mmqueue.md) — the Python `AXIMMQueue` model this
  hook is the synthesizable half of.
- [VMAC code generation](../../examples/mmqueue/codegen.md) — the generated top that
  calls `queue_get`, and the worked end-to-end example.
- [Writing a hook](./writing.md) / [Kernel transfer reference](./reference.md) — the
  general `.tpp` contract and the in-kernel transfer calls.
- [Polling Overhead](../interface/poll.md) — the loosely-timed poll model behind the
  empty-ring wait.
