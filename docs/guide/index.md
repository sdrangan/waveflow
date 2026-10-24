---
title: Guide
parent: Waveflow
nav_order: 2
has_children: true
summary: "How the guide is organized: the flow first. The Flow Overview walks one design through the whole flow, a page per step -- designing the DUT (with the stream patterns and the command-response protocol), simulating it in Python, simulating it at RTL, verifying it, calibrating its timing and resources, exploring the design space -- and the sections after it are the reference those pages link into, from the outside in (modules, interfaces, schemas, simulation, code generation, hooks, build), then the libraries, timing and resources, and the legacy flows last."
---
# Guide

Welcome to Waveflow.  This folder will have guides to use the Waveflow functionality as we develop it.

## How this guide is organized

**Waveflow models concurrent systems**, and the guide starts with the flow that takes one from an idea
to verified, measured hardware. The [Flow Overview](./systems/) walks it a page per step, on one running
example: [designing the DUT](./systems/dut.md) -- its modules, interfaces and messages, the
[stream patterns](./systems/streams.md) they talk by and the [command-response](./systems/command_response.md)
protocol most designs speak -- then [simulating it in Python](./systems/pysim.md) beside anything else the
question needs, [simulating it at RTL](./systems/rtl.md), [verifying](./systems/verification.md) it,
[calibrating](./systems/calibration.md) its timing and resources, and [exploring](./systems/dse.md) the
design space. Each step is done module by module: any module can be the DUT of its own harness.

The sections after it are the reference those pages link into, from the outside in:
[Modules](./flows/), [Interfaces](./interface/), [Data schemas](./schema/) and
[Vectorization](./vectorization/), the [Simulation](./sim/) engine, and how a module becomes hardware --
[Module Code Generation](./comp_codegen/) (what a tool writes for you), [Custom Hooks](./custom_hooks/)
(what no generator can guess) and the [Build System](./build/). Then the libraries
([Stream Blocks](./streams/), memory, RF, DSP), timing and resources, and, last, the
[Legacy flows](./legacy/).

Most readers -- people and agents alike -- arrive at a page from a search or a link, not from the top. So
every page says where it sits and links across, rather than restating a neighbour.

One correction worth making early, because the natural assumption is wrong. Code generation covers
**both** backends, and they are the **same dispatch**: `kind_of_endpoint` produces one vocabulary --
`axis_in`, `axis_out`, `maxi_read`, `maxi_write`, `mm_slave`, `axilite_slave`, `bram` -- and two tables
consume it, the boundary-port emitter for HLS and `BFM_DUALS` for the XSI testbench. So the C++ model
that drives a port from *outside* the kernel is as automatic as the port itself, and a module's harness
needs no testbench code at all.

The table of contents below is in reading order, and every entry's summary is read from that
section's own front matter — so the list is generated rather than maintained here, and cannot fall
out of step with what it lists.
