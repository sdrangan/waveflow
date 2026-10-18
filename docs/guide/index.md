---
title: Guide
parent: Waveflow
nav_order: 2
has_children: true
summary: "How the guide is organized: system first. Systems is the top -- a testbench and a DUT simulated together, the DUT a tree of modules, any module the DUT of its own harness -- with the recipe for building one; the sections after it are what that picture is made of, from the outside in (patterns, modules and the flow, interfaces, schemas, simulation, code generation, hooks, build), then the libraries, timing and resources, and the legacy flows last."
---
# Guide

Welcome to Waveflow.  This folder will have guides to use the Waveflow functionality as we develop it.

## How this guide is organized

**Waveflow models concurrent systems**, and the guide starts there. [Systems](./systems/) is the top: a
system is a testbench and a DUT simulated together, in pysim and at RTL; the DUT is a tree of modules
joined by interfaces carrying schemas; and any module can be made the DUT of its own system -- its
harness -- so a design is built module by module. [Building a system](./systems/recipe.md) is the recipe:
decompose top-down, write bottom-up, verify each module alone and then together.

The sections after it are what that picture is made of, from the outside in, each a reference a page of
Systems links into: [Design patterns](./patterns/) (the shapes accelerators take), the modules and the
flow ([Hardware modules and Flows](./flows/)), [Interfaces](./interface/), [Data schemas](./schema/) and
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
