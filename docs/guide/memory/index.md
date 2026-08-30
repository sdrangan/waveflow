---
title: Memory Modeling
parent: Guide
nav_order: 10
has_children: true
summary: "Storage in Waveflow is six categories ordered by the SCOPE OF SHARING — not by size and not by lifetime. Local temporaries and per-module persistent state (HwState) are inside one module; a BramMod is shared between modules but inside the top; AXI-MM (MemoryMod, MemMgr) is outside it; and two categories are storage the tool creates for you — a channel's FIFO depth, and the ping-pong buffer Vitis builds whether or not you asked. Includes what each backend models and what csynth counts."
---

# Memory Modeling

Storage in Waveflow is not one class, and the axis that sorts it is not the obvious one:

> **The scope of sharing determines the category — not the size, and not the lifetime.**

A four-word FIFO and a megabyte of DDR are different categories because of *who can reach them*, not
because of how big they are. A tap array that persists for the life of the design and a temporary
that lives for one firing are the *same* category if only one module can see them.

| # | category | mechanism | who picks the storage class |
|---|---|---|---|
| 1 | local temporaries, one module | plain Python / plain C++ | Vitis, from the body |
| 2 | persistent, one module | [`HwState`](./hwstate.md) | Vitis + directives |
| 3 | **between modules, inside the top** | [`BramIF` + a memory module](../interface/primitive/bram.md) | **the designer — it is hand-written RTL** |
| 4 | outside the top | AXI-MM ([`MemoryMod`](./memorymod.md), [`MemMgr`](./memmgr.md)) | the platform |
| 5 | channel storage | [`StreamIF.depth`](../interface/primitive/stream.md) | Vitis, from the pragma |
| 6 | block handoff | [`stream_of_blocks`](../interface/primitive/sob.md) | Vitis — **implicitly, if you share an array between tasks** |

Categories 5 and 6 are the ones a reader is most likely to be surprised to find here, and both earned
their place the hard way.

**A FIFO is memory**, and it is the storage most designs have most of. `StreamIF.depth` lives under
[Interfaces](../interface/primitive/stream.md) because a channel is how you *use* it — which is precisely why
nobody noticed for a long time that **a boundary port's declared depth is silently discarded**
(Vitis gives a top-level argument the default depth of 2 whatever you write). If you think of depth
as memory, you ask where it is; if you think of it as a channel attribute, you do not.

**Category 6 is created whether or not you asked for it.** Share a local array between two
`hls::task` bodies and Vitis builds a ping-pong buffer with a synchronizing handshake — and the
handshake *stalls the writer*:

```
INFO: [HLS 200-741] Implementing PIPO rx_buf_r_RAM_T2P_BRAM_1R1W using a single memory
```

That is a storage decision the tool made for you, in silence, and you should meet it here rather than
in a netlist. It is also the reason category 3 exists at all — see
[BRAM: memory between modules](../interface/primitive/bram.md).

## Category 1 needs one sentence, or you will get it wrong

**The Python model is not a storage spec.** A numpy array in `run_iter` need not correspond to
anything in the RTL; only *functional behaviour* is contracted. `RfSampIngress` is the worked
example: it takes a whole burst per firing in pysim and relays one word per firing in hardware, and
both are correct because the words that come out are the words that went in.

So do not read a local array in a Python body as "this design has a buffer". If storage is part of
what you mean, say so with one of the other five categories.

## Choosing between them

The decision is almost always about **which boundary the storage sits inside**.

**Does one module own it, and does it have to survive between firings?** `HwState` — filter taps, an
accumulator, a line buffer. It becomes a `static` array inside the generated kernel. No protocol, no
transactions: a hook receives it and indexes it.

**Do two modules need to reach the same words, concurrently?** That is category 3, and it is the one
with a sharp edge: **it cannot live inside a Vitis kernel at all.** The memory becomes hand-written
Verilog beside the kernel, joined by a generated wrapper. The designer owns the correctness argument
— the tool will not arbitrate for you, and it will not warn you.

**Is the storage on the far side of a bus?** `MemoryMod`, reached over `m_axi` — transactions,
latency, contention with other masters. In a real system this is DDR; in simulation a `MemoryMod`; in
a generated XSI testbench the arena the AXI-MM slave models serve out of.

**Are you deciding *where* things go rather than storing them?** `MemMgr` — the allocator. It answers
"where does this fit" and "what word index is this address", and it holds no bytes at all.

One neighbour worth naming so you do not reach for it by mistake: a **regmap** field is what the
*host* writes over AXI-Lite. Neither `HwState` nor `MemoryMod` is host-visible; a regmap is the
control-plane story, not the storage story.

`Region` is not a seventh category. It is a cross-cutting **access view** — element coordinates over
word storage — and it applies to several of the above.

## What each backend actually models

Neither backend is uniformly better, and knowing which is which stops you trusting the wrong one:

| category | pysim | XSI (RTL) |
|---|---|---|
| 5 · FIFO depth | honours the declaration everywhere | real **only for internal channels**; a boundary port's depth is discarded |
| 4 · AXI-MM | `BusCalib` timing **and** crossbar contention | an **un-arbitrated** `FlatMemory` — no contention modelled |
| 3 · BRAM | faithful (untimed: deterministic access) | faithful (the memory's published read latency) |

**pysim is the better memory-system model; XSI is the better fabric model.** The AXI-MM row is the
one that bites: a design whose masters fight for bandwidth looks clean under XSI and honest under
pysim, while a design whose boundary FIFO is deeper than 2 looks fine in pysim and is not.

## What `csynth` counts

| category | counted by `csynth` of the kernel? |
|---|---|
| 1 · local temporaries | **yes** |
| 2 · `HwState` | **yes** |
| 3 · memory beside the kernel | **no** — it is outside the kernel entirely |
| 4 · AXI-MM | **no** — it is off-chip |
| 5 · channel storage | **yes** |
| 6 · block handoff | **yes** |

Two of the six are invisible to the report, and category 3's absence is total rather than
approximate: a design with a 1024×16 buffer beside its kernel reports **no BRAM at all**. That is why
a structural block declares its own footprint (depth × width maps to a primitive count by geometry),
and why the [wrapper is the design scope](../comp_codegen/freerunning_composite.md) a resource
estimate can be defined against. Logic blocks cannot declare theirs and need a run — the same line as
*who owns the correctness argument*, one level up.

## One rule worth knowing up front

`MemMgr` is *handed* the occupied ranges rather than tracking them. The byte store stays the single
source of truth about what is occupied, so the manager and the storage can never disagree — a
parallel allocation table would be a drift bug waiting to happen. The visible consequence is that
freeing a region reopens its gap to the very next allocation.

The name is shared with the C++ side on purpose: the generated testbench uses `MemMgr<word_dwidth>`
(`memmgr_tb.hpp`) and kernels use the `waveflow::memmgr` namespace for the same conversions.

## Pages

- [`HwState`](./hwstate.md) — category 2: storage inside a module, what it emits, partitioning.
- [`MemMgr`](./memmgr.md) — allocation and addressing, including the byte-vs-word convention.
- [`MemoryMod`](./memorymod.md) — category 4: the transactional, timed memory, its latency model, and
  the `Memory` store underneath it.
- [Streaming Memory Kernels](./memstream.md) — `MemRStream` / `MemWStream` / `MemCopy`.

Category 3's own page is not written yet; until it is, [BRAM — memory between
modules](../interface/primitive/bram.md) is the reference, with
[A module realized as Verilog](../comp_codegen/rtl_module.md) for how the memory is declared.

Underneath categories 3 and 4 sits `Memory`, the sparse byte container. It is a plain Python object,
not a `SimObj` — the same category as `DataSchema` or `Region`. That is deliberate: in Waveflow
*`SimObj` means "participates in the discrete-event simulation"*, and a bag of bytes does not.
`MemoryMod` is what makes a `Memory` a participant.

Two neighbouring topics live outside this section on purpose, because they are not about storage:
how an endpoint becomes an HLS port (`m_axi`, `axis`, `s_axilite`) is
[Endpoint interfaces](../comp_codegen/interface.md), and packing typed arrays into words is
[Array serialization](../vectorization/hls/arrayutils.md).

Runnable toys for `HwState`, `MemMgr` and `MemoryMod` live in
[`examples/memory/`](https://github.com/sdrangan/pysilicon/tree/main/examples/memory) and are
executed by `tests/examples/test_memory_demos.py`, so the code on these pages cannot silently rot.
