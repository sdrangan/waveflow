---
title: Guide
parent: Waveflow
nav_order: 2
has_children: true
---
# Guide

Welcome to Waveflow.  This folder will have guides to use the Waveflow functionality as we develop it.

## How this guide is organized

The sections progress by **layer** — schema, vectorization, simulation, interfaces, flows, code
generation, hooks — and the last three of those are one arc read three times over, from three
positions:

- **Model it** — [Interfaces](./interface/) is the Python transactional model. What a port *is*, what
  its master and slave endpoints do, and how a transfer is timed in the SimPy simulation. Everything
  here runs in Python and nothing here is a synthesizable artifact by itself.
- **Generate it** — [Module Code Generation](./comp_codegen/) is what a tool writes for you from that
  model: the top-level function, the `#pragma HLS INTERFACE` directives, the regmap struct, the
  testbench harness. Mechanical, and therefore automatic.
- **Hand-write it** — [Custom Hooks](./custom_hooks/) is what no generator can guess: the datapath
  body behind a `@synthesizable` boundary, and the behavioral models that stand in for a real
  neighbour in simulation.

The same `StreamIF` appears in all three, which is why looking for "streams" in one section alone
finds a hole that is really material in another. A page states its own layer and links across, rather
than restating a neighbour's — restating is what produces context-free arcana.

The table of contents below is in reading order, and every entry's summary is read from that
section's own front matter — so the list is generated rather than maintained here, and cannot fall
out of step with what it lists.
