---
title: Flow Overview
parent: Guide
nav_order: 1.2
has_children: true
audience: python
summary: "How to build a system with Waveflow, start to finish -- where the guide starts. Waveflow models concurrent systems, and this section walks one design through the whole flow of building a system, a page per step -- designing the DUT (its modules, interfaces and messages; the stream patterns; the command-response protocol), simulating it in Python, simulating it at RTL, verifying it, calibrating its timing and resources, and exploring the design space. Two levels of simulation: pysim runs the DUT in a Simulation beside any SimObj -- a host, a wireless channel, a sensor; RTL simulation runs it in an HwSystem(sim, dut), whose testbench is the part of the simulation that has an RTL twin. One running example (the free-running polynomial) on every page, with steps up to a pipeline (block_eq) and a bus system (markov)."
---

# Flow Overview

**Waveflow models concurrent systems.** A design is several things running at once -- kernels,
memories, a host program, the world around them -- joined by streams and buses, and the questions worth
asking are about the whole: does it compute the right answer, how fast, at what cost. Waveflow describes
the hardware once, in Python, and runs that one description in a fast Python simulation and, cycle by
cycle, as RTL.

This section is how to build a system: the flow, a page per step, in the order a design takes them. Each page walks the same
running example and links down to the reference sections for the details.

## Build a system, step by step {#the-flow}

| step | the question | page |
|---|---|---|
| **Design the DUT** | what is built and how it is tested; what it does, its modules, interfaces and messages | [The DUT and two levels of simulation](dut.md) |
| | how modules talk on streams | [Stream patterns](streams.md) |
| | how a job goes in and its answer comes out | [Command-response](command_response.md) |
| **Simulate in Python** | does the design work, as part of everything around it | [Simulating in Python](pysim.md) |
| **Simulate at RTL** | does the hardware do what the Python does, cycle by cycle | [Simulating at RTL](rtl.md) |
| **Verify** | what is compared with what, and what a passing gate proves | [Functional verification](verification.md) |
| **Calibrate** | how long it takes and what it costs, measured per module and composed | [Timing and resources](calibration.md) |
| **Explore** | which design, of the ones that work | [Design-space exploration](dse.md) |

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
