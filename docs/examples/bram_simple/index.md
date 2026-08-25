---
title: Shared memory between two modules
parent: Examples
nav_order: 9.5
has_children: true
summary: "Two free-running tasks over one true-dual-port memory that lives OUTSIDE the kernel, as hand-written Verilog joined by a generated wrapper — the structure Vitis leaves no alternative to. Command-driven: each side takes a (pointer, count) command and answers, because a write has no return path and a refused read is indistinguishable from a quiet stream. Reproduces a witness that ran before any of this infrastructure existed, at a width where the byte/word address convention can actually fail, and overlaps a write with a read on purpose."
---

# Shared memory between two modules

Two `hls::task` bodies sharing a buffer is the natural way to write a capture buffer, a scoreboard, a
reorder queue. **Inside a Vitis kernel it has no expression**: a local array crossing two tasks
becomes a synchronizing PIPO channel whose handshake stalls the writer, and one `bram` port used both
ways is a hard error. The memory has to live *beside* the kernel, as hand-written Verilog, with a
generated wrapper joining the two — and this example is the smallest complete design that does it.

The vehicle is deliberately **domain-free**. `BramIF` is used by every RF buffer in the tree, but a
reader who wants "shared memory between two modules" for something else should not have to read an RF
example to see it. What is here is a memory, a writer, a reader, and nothing else:

```
cmd_w  ──▶ ┌──────────────┐ ──buf_w──▶ ┌──────────┐
data_w ──▶ │ BramWriteCmd │            │  T2pBram │   hand-written Verilog,
resp_w ◀── └──────────────┘            │          │   BESIDE the kernel
cmd_r  ──▶ ┌──────────────┐ ──buf_r──▶ │          │
data_r ◀── │ BramReadCmd  │ ◀──────────└──────────┘
resp_r ◀── └──────────────┘
```

The duplication with [`RfShotBuf`](../../guide/rf/) is a feature rather than a cost: seeing one
primitive carry two unrelated designs is the point of having a primitive.

## What you will learn

- How to create a memory and connect two `HwModule`s to it through a `BramIF` — and why it is
  registered with `add_rtl_if` rather than `add_if`.
- How to read and write it concurrently, and **what actually guards the hazard** — which is not the
  thing the memory's own Verilog appears to promise.
- How to run the design in Python simulation and record its timing.
- How to add timing for the BRAM **read path** in the Python model, and why the number must never be
  typed into Python.
- How to run an RTL simulation in which the memory is the real hand-written Verilog, with no BFM
  standing in for it.
- How to verify throughput and overlap from a timing diagram built out of the RTL trace.
- How the two backends' timing compares — where they agree for free, and the one place they do not.

## The pages

- [Overview](overview.md) — what a BRAM is here, why it cannot be inside the kernel, and the topology.
- [Python model](python.md) — the two tasks, the composite, and how the read-path delay is added.
- [Python simulation](pysim.md) — running it, the test vectors, and recording the timing.
- [Code generation](codegen.md) — the kernel, `bram_t2p.v`, and the wrapper that joins them.
- [RTL simulation](rtlsim.md) — running XSI, and producing the trace.
- [Reading the trace](timing.md) — the activity diagram, the hazard scan, and the comparison to pysim.

## Scenario zero is a witness, and its numbers are not ours

`plans/witness/t2p_bram/` is four hand-written files — a kernel, a memory, a wrapper and a testbench —
that were synthesized and simulated **before any of this infrastructure existed**. They wrote
`buf[i] = i + 100` for 256 words, then read addresses `0, 1, 7, 255, 128`, and got back
`100, 101, 107, 355, 228`.

That is the only gate in this repo checking Waveflow against something built independently of
Waveflow, and this example subsumes it: the witness is one `write(wp=0, nwords=256)` followed by five
one-word reads. Both backends reproduce all five.

A **ramp rather than a constant**, deliberately. The likeliest failure in a design like this is a
read-latency mismatch between the kernel's `latency=` pragma and the memory's published
`READ_LATENCY`, which shifts every returned value by one position — and sails through a constant
check without a murmur.

## Two things this example exists to say out loud

**The geometry has to wrap.** The gated configuration is **64-bit** words. Vitis byte-addresses a
`mode=bram` port, so a 1024-word memory at 64 bits is reachable at only 128 distinct addresses unless
the wrapper undoes the scaling — and a design that never addresses past 128 round-trips perfectly
either way. Its retired predecessor, `bram_toy`, filled 256 of 1024 words at *sixteen* bits and
stayed green straight through a defect that had every BRAM design in the tree mis-addressed. At 64
bits the same 256 words reach byte address 2040, and word 128 onward aliases immediately. The
convention is written up in the [interface guide](../../guide/interface/bram.md#the-addressing-convention).

**The memory's `$error` fires, and nothing in this flow can hear it.** `bram_t2p.v` asserts its own
invariant — the reader must never touch the word the writer is writing — and in the XSI flow this
repo runs, RTL text output is discarded entirely. So the hazard is detected in the **waveform**
instead, and the gate is a *pair*: scenario zero must come back clean, and a scenario built to
collide must come back dirty. [Reading the trace](timing.md) is where that is done.

## See also

- [BRAM — memory between modules](../../guide/interface/bram.md) — the interface, the evidence for
  why the memory cannot be inside a kernel, and the addressing convention.
- [A module realized as Verilog](../../guide/comp_codegen/rtl_module.md) — the `rtl_module()` hook,
  the port-name chain, and the latency single-source rule.
- [Free-running composite](../../guide/comp_codegen/freerunning_composite.md) — where the wrapper
  fits, and what `csynth` does *not* count.
