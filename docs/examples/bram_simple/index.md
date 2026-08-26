---
title: Shared memory between two modules
parent: Examples
nav_order: 9.5
has_children: true
summary: "Two free-running tasks over one true-dual-port memory that lives OUTSIDE the kernel, as hand-written Verilog joined by a generated wrapper — the structure Vitis leaves no alternative to. Command-driven, over four DataList messages that generate the C++ headers the kernel compiles against: each side takes a (tid, nsamp, addr) command and answers with a (tid, status) response, because a write has no return path and a refused read is indistinguishable from a quiet stream. Reproduces a witness that ran before any of this infrastructure existed, at a width where the byte/word address convention can actually fail, and overlaps a write with a read on purpose."
---

# Shared memory between two modules

Sharing memory between concurrent tasks arises in a multitude of applications — capture buffers,
scoreboards, and storage of intermediate values. As the [memory guide](../../guide/memory/) describes,
there are three ways to share memory between hardware modules:

- **External** memory, typically DDR, that exists on the board and reaches the programmable logic
  over an AXI-MM interface.
- A **ping-pong buffer** (PIPO), which transfers blocks with a synchronization mechanism built in.
- A dedicated **BRAM** (Block RAM), instantiated in the top-level design in the programmable logic.

This example demonstrates implementing, modelling and using a **BRAM**, where two hardware modules
share one true-dual-port memory. For shared DDR see the [histogram example](../shared_mem/); for the
ping-pong buffer, the [interleaver example](../interleaver/).

## Why a dedicated BRAM?

A BRAM can be given **dedicated** access to a small number of hardware modules. DDR usually has to be
shared with other modules and with the PS, which can add substantial delay.

A PIPO is also typically built from a two-port BRAM, but it comes with a synchronization mechanism
you do not choose, and it requires one module to write a *different buffer segment* than another
reads. A BRAM gives you the memory and leaves the correctness argument to you — which is the whole
subject of this example.

## A simple example

In this example, we build the simplest example of two hardware modules sharing a BRAM.

![Two tasks, BramWriteCmd and BramReadCmd, inside the bram_simple Vitis kernel; the bram_t2p memory beside the kernel but inside the bram_simple_top wrapper, reached over buf_w and buf_r bram ports; six streams on the left carrying commands, payload and responses.](figures/bram_simple_topology.svg)

**The nesting is the point.** The memory is *outside* the kernel and *inside* the wrapper, because a
memory shared between two tasks has no expression inside a Vitis kernel at all — the
[overview](overview.md) has the evidence. Each side takes a `(tid, nsamp, addr)` command and answers
with a `(tid, status)` response: a write has no return path of its own, and a refused read returns
zero words, which on a stream is indistinguishable from "not yet". All four messages are declared
once as schemas, and the C++ headers the kernel compiles against are generated from them.

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

Once you have this example, the same structure is what the
[RF shot buffer](../../guide/rf/rfshotbuf/) is built on.

## The pages

- [Overview](overview.md) — what a BRAM is here, why it cannot be inside the kernel, and the topology.
- [Python model](python.md) — the two tasks, the composite, and how the read-path delay is added.
- [Python simulation](pysim.md) — running it, the test vectors, and recording the timing.
- [Code generation](codegen.md) — the kernel, `bram_t2p.v`, and the wrapper that joins them.
- [RTL simulation](rtlsim.md) — running XSI, and producing the trace.
- [Reading the trace](timing.md) — the activity diagram, the hazard scan, and the comparison to pysim.

## See also

- [BRAM — memory between modules](../../guide/interface/bram.md) — the interface, the evidence for
  why the memory cannot be inside a kernel, and the addressing convention.
- [A module realized as Verilog](../../guide/comp_codegen/rtl_module.md) — the `rtl_module()` hook,
  the port-name chain, and the latency single-source rule.
- [Free-running composite](../../guide/comp_codegen/freerunning_composite.md) — where the wrapper
  fits, and what `csynth` does *not* count.
