---
title: Flow Overview
parent: Waveflow Architecture
nav_order: 0.5
audience: python
summary: "How to build a system with Waveflow, start to finish, a page per step: designing the DUT (what it does as a golden, its modules, interfaces and messages; the stream patterns; the command-response protocol; each kernel's Python model and HLS body), simulating it in Python, simulating it at RTL, verifying it, calibrating its timing and resources, and exploring the design space. What is built -- the levels of simulation and the DUT's tree -- is on Waveflow Architecture, read first. The HLS bodies are needed only from the RTL step on. One running example (the free-running polynomial) on every page, with steps up to a pipeline (block_eq) and a bus system (markov)."
---

# Flow Overview

This section is how to build a system: the flow, a page per step, in the order a design takes them.
What is being built -- the three levels of simulation, the DUT's tree, what you write and what Waveflow
provides, and the terms -- is [Waveflow Architecture](index.md): read it first. Each page walks the same
running example and links down to the reference sections for the details.

## Build a system, step by step {#the-flow}

| step | the question | page |
|---|---|---|
| **Design the DUT** | what it does (the golden), its modules, interfaces and messages | [Designing the DUT](dut.md) |
| | how modules talk on streams | [Stream patterns](streams.md) |
| | how a job goes in and its answer comes out | [Command-response](command_response.md) |
| | the kernel, twice: a Python model and an HLS body that must agree | [Defining the kernel in Python and HLS](hwmodule.md) |
| **Simulate in Python** | does the design work, as part of everything around it | [Simulating in Python](pysim.md) |
| **Simulate at RTL** | does the hardware do what the Python does, cycle by cycle | [Simulating at RTL](rtl.md) |
| **Verify** | what is compared with what, and what a passing gate proves | [Functional verification](verification.md) |
| **Calibrate** | how long it takes and what it costs, measured per module and composed | [Timing and resources](calibration.md) |
| **Explore** | which design, of the ones that work | [Design-space exploration](dse.md) |

**The HLS bodies come when the hardware does.** Each kernel's Python model is written while designing
the DUT and is all pysim needs; its HLS body is first needed at [Simulating at RTL](rtl.md), and again
for the timing and resources measured from synthesis. A study that stays in Python needs none.

The steps are done module by module: any module in the design can be the design under test of its own
system, its **harness**, so each is verified and measured alone before the whole is put together
([Simulating in Python](pysim.md#the-cut)).

## The running example

Every page walks the **[free-running polynomial](../../examples/stream_kernel/index.md)**: a kernel that
reads a command and its samples on a stream, evaluates a polynomial, and answers on two more streams.
Its arithmetic is trivial on purpose, so each page is about the flow. Where a page's idea needs more,
it takes a step up to a pipeline ([block_eq](../../examples/block_eq/index.md)) or a system on a bus
([markov](../../examples/markov/index.md)); the whole ladder of systems is on
[Simulating at RTL](rtl.md#a-ladder-of-systems).

The older host-launched flow -- one kernel called by a sequential Vitis testbench -- is under
[Legacy flows](../legacy/index.md). Host-activated *modules* are not legacy: they are devices a host
launches over the bus.
