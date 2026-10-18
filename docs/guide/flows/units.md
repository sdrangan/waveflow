---
title: Developing module by module
parent: Modules
nav_order: 3
has_children: true
audience: python
api: [HwSystem, harness, memory_harness, add_system_steps]
summary: "How a design of several modules is developed one module at a time. Any module in a design's tree can be run alone: harness(module) makes it the DUT of its own HwSystem, with a stand-in at each port generated from the port's type, each with an RTL twin, so the harness runs in pysim and at RTL. The pages: the harness and its stand-ins (streams, credit streams, memory-master ports, a memory's own slave port) and workload(kind); reading what a harness recorded (sinks, the memory's arena, arrival and acceptance cycles); hostile stand-ins (StandInPolicy, add_robustness_steps); calibrating a module in its harness; composing a parent from its units' records, checked against the parent's RTL, and incremental builds."
---

# Developing module by module

A design is a tree of modules -- kernels, memories, the devices that put kernels on a bus, the systems
that hold them. Developing it module by module means: run each module **alone**, verify it, measure
its timing, and then **compose** -- a parent's behavior and timing follow from its children's, checked
once against the parent's RTL. These pages are the practice; [Free-running polynomial](../../examples/stream_kernel/index.md)
is it on one kernel, and [Two kernels on a bus](../../examples/markov/index.md) on a system of several.

## A unit is any module in the tree

There is no unit class. Any `HwModule` in a design's DUT tree can be built and run alone -- a leaf
kernel, a memory, a composite of several. The unit tree **is** the module tree: a module's children,
boundary and wiring are already declared by the module itself.

What runs is always an [`HwSystem`](../systems/rtl.md#the-system): a simulation plus **one named DUT**.
The testbench is everything else in the simulation, derived, never listed. Each testbench participant
must have a lowering to the RTL testbench (its `bfm_model()`), so the same system runs in pysim and under
XSI.

## The practice, a page each

- [The harness](units_harness.md) -- `harness(module)`: the module made the DUT of its own system, a
  stand-in per port chosen by the port's type (streams, credit streams, memory-master ports), each with
  its RTL twin; a memory's own harness; the scenario the module supplies, `workload(kind)`.
- [Reading a harness's recordings](units_recordings.md) -- what each stand-in records, where a pysim run
  and an RTL run put it, and the cycles each word arrived at and was accepted at.
- [Hostile stand-ins](units_hostile.md) -- `StandInPolicy` and `add_robustness_steps`: the harness again
  with stalling sinks, held-off drivers, starved credit and late answers.
- [Calibrating in a harness](units_calibration.md) -- a module's timed phases fitted in its harness and
  filed under its identity.
- [Composing a parent](units_composing.md) -- a parent's pysim composed from its units' records, checked
  against its RTL; incremental builds.
