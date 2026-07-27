---
title: Memory Modeling
parent: Guide
nav_order: 10
has_children: true
summary: "Waveflow models storage with three objects, split by where the storage lives and who is responsible for it. HwState is storage inside a hardware module — codegen emits it, so it is synthesizable, and it has no transactional interface. MemMgr is allocation and address arithmetic, owning no bytes at all. MemoryMod is storage on the far side of a bus, reached transactionally and modelled with latency. Memory is the sparse byte container the latter two share."
---

# Memory Modeling

Storage in Waveflow is not one class. It is **three**, split by a question worth asking early about
any piece of memory in a design: *where does it live, and who is responsible for it?*

| | what it is | timed? | synthesizable? | interface |
|---|---|---|---|---|
| [`HwState`](./hwstate.md) | storage **inside** a hardware module | no — a hook's own timing | **yes** — codegen emits it | none; you index it |
| [`MemMgr`](./memmgr.md) | allocation + address arithmetic, **no bytes** | no | n/a — it is policy | n/a |
| [`MemoryMod`](./memorymod.md) | storage **across a bus** | yes — access latency | no; the *interface to it* is | transactional (AXI-MM) |

Underneath the last two sits [`Memory`](./python.md), the sparse byte container. It is a plain Python
object, not a `SimObj` — the same category as `DataSchema` or `Region`. That is deliberate rather
than an oversight: in Waveflow *`SimObj` means "participates in the discrete-event simulation"*, and
a bag of bytes does not. `MemoryMod` is what makes a `Memory` a participant.

## Choosing between them

The decision is almost always about the **kernel boundary**.

**Does the kernel own the storage?** Then it is `HwState`. Filter taps, an accumulator, a line
buffer, a small lookup table — things that become a `static` array inside the generated kernel and
persist across firings. There is no protocol to speak: a hook receives it and indexes it, and the
timing is whatever the surrounding hook does.

**Does the kernel reach out to the storage?** Then it is `MemoryMod`, and the kernel talks to it over
`m_axi` — transactions, latency, contention with other masters. In a real system this is DDR; in
simulation it is a `MemoryMod`; in a generated XSI testbench it becomes the arena the AXI-MM slave
models serve out of.

**Are you deciding *where* things go rather than storing them?** That is `MemMgr` — the allocator. It
answers "where does this fit" and "what word index is this address", and it holds nothing.

One more thing is worth naming so you do not reach for the wrong one: a **regmap** field is what the
*host* writes over AXI-Lite. Neither `HwState` nor `MemoryMod` is host-visible; a regmap is the
control-plane story, not the storage story.

## Why the split is worth an extra class

Two of these were once one. `Memory` owned the bytes *and* the allocation policy, and address
conversion lived as private methods on it — so there was no way to talk about a placement policy, or
reuse one, without dragging a backing store along. Pulling `MemMgr` out gives address conversion
exactly one implementation and makes the policy a thing you can name and test on its own.

`MemMgr` is *handed* the occupied ranges rather than tracking them. That is deliberate: the byte
store stays the single source of truth about what is occupied, so the manager and the storage can
never disagree. A parallel allocation table would be a drift bug waiting to happen.

The name is not new to the codebase — the C++ testbench side has used `MemMgr<word_dwidth>`
(`memmgr_tb.hpp`) and the `waveflow::memmgr` namespace all along. The Python class is the same idea
on the same side of the same fence.

## Pages

- [`HwState`](./hwstate.md) — storage inside a module: declaring it, what it emits, partitioning.
- [`MemMgr`](./memmgr.md) — allocation and addressing, including the byte-vs-word convention.
- [`MemoryMod`](./memorymod.md) — the transactional, timed memory and its latency model.
- [Using `Memory` in Python](./python.md) — the byte store itself: `alloc`, `read`, `write`.
- [Memory Interfaces in Vitis HLS](./vitis.md) — how a memory maps to `m_axi` and local arrays.
- [Streaming Memory Kernels](./memstream.md) — `MemRStream` / `MemWStream` / `MemCopy`.

Runnable toys for the three objects live in
[`examples/memory/`](https://github.com/sdrangan/pysilicon/tree/main/examples/memory) and are
executed by `tests/examples/test_memory_demos.py`, so the code on these pages cannot silently rot.
