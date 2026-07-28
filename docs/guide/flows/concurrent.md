---
title: Concurrent (free-running)
parent: Hardware modules and Flows
nav_order: 3
has_children: true
audience: python
summary: "Flow 2 — a free-running (ap_ctrl_none) kernel or composite Vitis cannot co-simulate, verified by driving the elaborated RTL cycle-by-cycle through a concurrent XSI BFM. The concept here; the full worked walkthrough is the mem_copy example."
---

# Concurrent (free-running) flow

The concurrent flow is for a DUT that **runs on its own**. The kernel is `ap_ctrl_none`: no start/done
handshake — one `hls::task` for a leaf, one per child for a composite, wired by internal channels. It
never waits to be launched and never returns; a leaf simply *re-fires* on each new job. In Python it is
a [`FreeRunMod`](./modules.md) — a leaf implements `run_iter`, a composite has sub-components.

Because it never returns, there is nothing for a sequential program to *call* — which changes both what
it can do and how it must be verified.

## Why free-running

A free-running design **overlaps work**. Jobs pipeline across it: the reads of job *j+1* can proceed
while job *j* is still being stored, and in a composite each stage runs concurrently with the others.
That is throughput the [sequential flow](./sequential.md) cannot get, because there the host serializes
one call at a time.

The cost is verification. Vitis C/RTL co-simulation drives a kernel through its `ap_start`/`ap_done`
handshake — and a free-running kernel has none, so **co-sim refuses it**. Verification instead drives
the elaborated RTL directly, cycle by cycle, through an **XSI BFM** (a cycle-based bus-functional
model). The BFM drives every port each cycle, so it *is* a concurrent harness — which is why there is
no separate SystemC flow. An earlier draft had one; it was refuted, because the XSI BFM already does
what a SystemC testbench would have been for.

The specific blocker is worth naming: an `ap_ctrl_none` block that also carries `m_axi` cannot be
Vitis co-simulated at all. The moment a DUT is free-running, verification drops a level to XSI.

## When to use it, and what it costs

**Use it when** the design is a dataflow/streaming accelerator, or any multi-task composite whose
stages should run concurrently — anything that benefits from pipelining rather than one-shot invocation.

- **+** True concurrency and cross-job pipelining — the throughput a host-serialized kernel cannot get.
- **+** Composites: a network of `hls::task` stages, each running independently.
- **−** No Vitis co-sim — verification is at RTL through a hand-built (but *generated*) XSI harness.
- **−** More machinery: internal channels, per-job tokens, and the RTL/XSI toolchain (xsim).

## How to read this flow

- **[Writing it in Python](./concurrent_python.md)** — how to describe the module: a leaf's
  `run_iter` (one firing) versus a composite's graph, and carrying state across firings.
- The **[flow steps](./concurrent_flowsteps.md)** page is the recipe — from the component graph to a
  generated top, generated XSI harness, and an exact cycle-count check.
- The **[mem_copy example](../../examples/memcpy/)** is the full worked walkthrough: the composite
  (`Sequencer → MemRStream → MemWStream`), the generated `ap_ctrl_none` top, the generated XSI
  testbench, and the RTL/XSI verification.
- The framework memory streamers it is built from are the
  [Streaming Memory Kernels](../memory/memstream.md) page.
- **[How it is realized in HLS](../comp_codegen/composite.md)** — the generated `ap_ctrl_none` top,
  the task bodies, and [the XSI testbench](../comp_codegen/xsi_tb.md).

The XSI gates are **exact cycle counts**, not bounds: a count that moves is either a real regression
or a real improvement, and both deserve a human look.

**Targets:** `composite_kernel` (the DUT — one target for a leaf and a composite alike, a leaf being
the 1-task case) + `sequential_xsi_tb` (the XSI testbench) — both built.
