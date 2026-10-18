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
| **Design the DUT** | what is built, what it does, its modules, interfaces and messages | [Designing the DUT](dut.md) |
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

## Two levels of simulation

The flow has two simulations of the same design, and they ask different questions:

- **pysim** runs the design in a **[`Simulation`](../sim/index.md)**: a discrete-event simulation of
  any collection of `SimObj`s. Beside the hardware there can be anything the question needs -- a host
  program, a wireless channel, a traffic source, a processor model. It is fast, its timing is a model,
  and it is where a design is explored and evaluated as part of a larger system.
- **RTL simulation** runs the design in an **[`HwSystem(sim, dut)`](rtl.md#the-system)**: the
  simulation with one module named the **DUT** -- what is synthesized -- and a **testbench** of
  everything else. The RTL of the DUT runs cycle by cycle under Vivado's simulator, and every testbench
  part runs as its C++ twin beside it.

The rule between them is one line: **anything can join a `Simulation`; only what has an RTL twin can
join an `HwSystem`.** A host has a C++ twin, so it can drive the RTL; a wireless channel model has
none, so a system with one is a pysim study, and its RTL simulation cuts at the channel's edge.

## A system, in one picture

![A generic system. A host (a SwHost, tan) is a bus master on an AXI crossbar (purple, vendor IP). The crossbar's slaves are three sub-tops, each in a dashed box. Device A holds a front with views (green, hand-written RTL), kernel A (blue, Vitis HLS) and a bus writer (blue) that acts for kernel A. Device B holds a front with views and kernel B, which also masters the bus through m_axi. The memory holds a front with a window and the RAM (both green). A solid box around the crossbar and the three sub-tops is the DUT; a dotted box around everything, host included, is the HwSystem. An interrupt line runs from device A's front across the DUT boundary to the host. A legend names the build step that realizes each colour: csynth for blue, system_rtl for green, xbar_ip for purple, system_xsi for tan.](../flows/figures/system_cut.svg)

Read it from the outside in:

1. **The system** (dotted) is everything simulated together: an `HwSystem`, a simulation plus one
   named DUT. It is flat -- a testbench and a DUT, nothing nested.
2. **The testbench** is everything outside the DUT, derived, never listed: here the host, a
   [`SwHost`](../build/sw_threads.md) whose threads are Python in pysim and a C++ twin at RTL. In a
   smaller system it is stream drivers and sinks.
3. **The DUT** (solid) is what is synthesized: a **tree of modules** -- here three devices and the
   crossbar between them.
4. **A device** (dashed) is a kernel with everything that belongs to it -- its memory-mapped views
   and front, the bus writers that act for it -- one IP when the system is packaged.
5. **A module** is a [`HwModule`](../flows/modules.md): typed ports and a behavior. A **kernel** is a
   free-running module whose behavior is a Python `run_iter` and, at RTL, an HLS body.
6. **The interfaces** between modules are typed connections -- streams, credit streams, AXI-MM,
   BRAM, interrupts ([Interfaces](../interface/index.md)).
7. **The messages** on them are [schemas](../schema/index.md): the fields once, in Python, and every
   word packed by generated code.

The colours are the ways a part becomes hardware: **blue** is Vitis HLS (csynth), **green** is
hand-written framework RTL, **purple** is vendor IP, **tan** is the testbench (never synthesized).

## The running example, and the steps up

Every page walks the **[free-running polynomial](../../examples/stream_kernel/index.md)**: a kernel that
reads a command and its samples on a stream, evaluates a polynomial, and answers on two more streams.
Its arithmetic is trivial on purpose, so each page is about the flow. Where a page's idea needs more,
it takes a step up:

| system | the DUT | the testbench | worked example |
|---|---|---|---|
| one kernel on streams | a free-running kernel | its harness: a stream driver and sinks | [Free-running polynomial](../../examples/stream_kernel/index.md) |
| a pipeline of kernels on streams | a composite: a broadcast and two stages, joined by internal streams | its harness; each stage alone in its own | [Block equalizer](../../examples/block_eq/index.md) |
| one kernel on a bus | the same kernel as a device, and the crossbar | a two-thread `SwHost` | [Behind AXI-MM](../../examples/stream_kernel/memory_mapped.md) |
| a pipeline with memory | a composite of free-running tasks, memory through stream adaptors | stream drivers, sinks, a memory model | [memcpy](../../examples/memcpy/index.md), [interleaver](../../examples/interleaver/index.md) |
| several kernels, a memory, a host | devices, a memory, the crossbar | a `SwHost` | [Two kernels on a bus](../../examples/markov/index.md) |

The older host-launched flow -- one kernel called by a sequential Vitis testbench -- is under
[Legacy flows](../legacy/index.md). Host-activated *modules* are not legacy: they are devices a host
launches over the bus.
