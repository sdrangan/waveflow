---
title: MemoryMod — storage across a bus
parent: Memory Modeling
nav_order: 3
summary: "MemoryMod is a SimObj that wraps a Memory and exposes AXI-MM endpoints, so storage on the far side of a bus becomes a real simulation participant with modelled access latency. Access latency (the memory) and bus latency (the interconnect) compose rather than being double-counted. In a generated XSI testbench it maps to the FlatMemory arena the AXI-MM slave models serve out of."
---

# `MemoryMod` — storage across a bus

`MemoryMod` is the far side of the kernel boundary. It wraps a [`Memory`](./python.md), makes it a
`SimObj`, and gives it AXI-MM endpoints — so storage the kernel *reaches out to* becomes a real
participant in the discrete-event simulation, with latency, contention, and transactions.

This is the class that answers "what is a bag of bytes doing in my simulation?" — nothing, until a
`MemoryMod` wraps it.

```python
from waveflow.hw.memory import MemoryMod

mem = MemoryMod(name="ddr", sim=sim, clk=clk, inline=False,
                word_size=64, nwords_tot=8192,
                latency_init=10, latency_per_word=1)
addr = mem.alloc(256)
```

## Two ports, two stories

**`s_mm`** is an `MMIFSlave` — the AXI-MM port an external master connects to. This is the path that
models latency, and the one a kernel's `m_axi` reaches across.

**`m_mm`** is a directly-backed master, **zero latency**, for the owner's own use. It models a
component reading its *own* inline block — a local C array in HLS — so no bus or access delay
applies. `m_mm.as_words()` / `as_array()` / `as_schema()` return direct views.

The `inline` flag picks which story you are telling. `inline=True` pre-allocates the full capacity as
one block and hands out direct views; `inline=False` is the external-DDR shape, where callers
`alloc()` regions and reach them over the bus.

> **A note on `inline=True`.** It models storage the owner treats as local. If what you actually want
> is storage the *generated kernel emits and owns*, that is [`HwState`](./hwstate.md) — codegen emits
> a `static` for it, whereas nothing is emitted for a `MemoryMod`. `MemoryMod` is a simulation and
> testbench object; the kernel-side counterpart is `HwState`.

## The latency model, and what composes

The memory models **access** latency; the interconnect models **bus** latency; the two **compose**
and are not double-counted. Each access on the `s_mm` path consumes

```
(latency_init + nwords * latency_per_word) / clk.freq
```

simulation seconds before touching the backing store. The interconnect adds its own request and
return latency *around* that callback, so a read's total time is

```
bus_request + memory_access + bus_return
```

`half_duplex=True` makes the slave's read and write channels one shared resource, so reads and writes
to this memory mutually exclude — a single-port memory, or a DDR model that shares R/W bandwidth. The
default is full duplex, with independent AR/R and AW/W channels, which is what real AXI gives you.

## In a generated testbench

A `MemoryMod` maps to a `FlatMemory` in the generated XSI testbench: the arena the AXI-MM slave
models serve out of. It is declared `shared`, which matters — two `m_axi` bundles (a `gmem0` read and
a `gmem1` write) backed by *one* memory means the emitter constructs the arena once and hands it to
both slave models, rather than making one per bundle.

Its `load_segs` / `dump_segs` are `DynParam`s: regions loaded from burst bundles at `pre_sim` and
dumped back at `post_sim`. Both backends read the same bundles, so the pysim run and the RTL run are
driven from one scenario rather than two restatements of it.

```python
mem.load_segs = [MemSeg(0, 0, "vectors/mem_in")]
mem.dump_segs = [MemSeg(0, nwords_tot, "vectors/out")]
```

## Contention is modelled where it belongs

Two masters on one memory serialize, and that is the interconnect's job, not the memory's — an
`AXIMMCrossBarIF` models the arbitration. Worth knowing: the pysim crossbar models contention, while
the XSI slave models do not. The two describe different systems on purpose, so a cycle count from one
is not a prediction of the other.

## What it is not

- **Not synthesizable.** Nothing is emitted for a `MemoryMod`; what *is* synthesizable is the
  kernel's `m_axi` interface to it. If you need storage the kernel owns and codegen emits, use
  [`HwState`](./hwstate.md).
- **Not the allocator.** Its `alloc` / `free` forward to the wrapped `Memory`, which delegates
  placement to a [`MemMgr`](./memmgr.md). One policy, one implementation.
- **Not the bytes.** That is [`Memory`](./python.md), which a `MemoryMod` wraps and which is
  perfectly usable on its own when you only need a store and no simulation.
