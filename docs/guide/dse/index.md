---
title: Agentic DSE
parent: Guide
nav_order: 12.8
has_children: true
audience: python
summary: "Design-space exploration by an AI agent: given a fixed architecture, choose its parameters to meet an objective within a budget of tool time, using an untimed bit-exact simulation, synthesis, RTL simulation and, in Waveflow, pysim. What DSE is, why this section covers the parametric kind, and the running example (the CG detector, examples/dse_cg). Under construction."
---

# Agentic DSE

**Under construction.** This section is being written alongside the experiment it describes
([`examples/dse_cg`](../../../examples/dse_cg/README.md), plan
[`plans/dse_cg.md`](../../../plans/dse_cg.md)). The interfaces are settled; most of the code is not
written yet.

## What design-space exploration is

A hardware design has choices: how many multipliers, how wide a word, how deep a queue. Each set of
choices gives a different design with a different area, speed and accuracy. **Design-space
exploration (DSE)** is the search for the set of choices that best meets a goal, such as "the
smallest design that solves a system in 4,000 cycles with an error below −20 dB".

DSE is hard for two reasons. The space is large: ten parameters with five values each is about ten
million designs. And the evaluations that give the true answer are slow. A synthesis takes minutes
to hours, and an RTL simulation adds more. Nobody can measure every design, so the skill is in deciding
which few to measure.

## Parametric DSE

DSE comes in two kinds:

- **Architectural** DSE changes the structure: which blocks exist, how they connect, what runs in
  hardware and what in software.
- **Parametric** DSE keeps the structure fixed and searches over its parameters: the sizes, widths
  and depths of blocks that are already there.

This section covers **parametric** DSE only. The architecture is given, and the search is over the
values of its build parameters (`HwParam`, see [Parametrization](../flows/parametrization.md)). That
is the kind a calibrated model can predict well, since every point is the same blocks at other sizes,
and the kind most design teams spend their time on once an architecture is chosen.

## Who explores: an agent

The explorer here is an **AI agent**: a language model working in a loop, calling tools, reading
what comes back and deciding what to try next. It works like an engineer with a budget of tool time.
It cannot measure everything, so it has to reason about which designs are worth measuring.

The question this section is built around is whether **pysim** helps. pysim is Waveflow's
bit-exact, loosely-timed simulation with calibrated cycle and resource models
([Model calibration](../calib/index.md)). A traditional flow has two kinds of evaluation:

- a systems team's **untimed bit-exact simulation**, which gives accuracy but no timing or area;
- a hardware team's **synthesis and RTL simulation**, which give timing and area but are slow.

Waveflow adds a third kind in between: pysim gives exact accuracy and *approximate* timing and area,
in seconds. The experiment gives an agent the same task twice, once without pysim and once with it,
and measures how much tool time each needs to find a good design.

## The running example

The running example is the **CG detector** of [`examples/mimo_cg`](../../examples/mimo_cg/index.md):
a conjugate-gradient solver built from a systolic array and a vector unit, with ten build parameters
(array rows and columns, lanes, word width, queue depths, ...). The DSE work on it lives in
[`examples/dse_cg`](../../../examples/dse_cg/README.md).

## The pages

- [The DSE problem](problem.md): what is being searched, what is measured, what the goal is, and
  how all of that is written down for an agent.
- [The design context](context.md): how `DSE.md`, the file that describes one design to the agent,
  is generated from the design's Python.
- [The service](service.md): the process behind every evaluation: why the adaptor is hidden, the
  `dselib` functions, what the service checks and charges, the ledger, and the live and replay
  backends.
- [Skills](skill.md): how the agent is given the method for using all of this, and how it reads it.
  If you have not worked with AI agents before, its first sections stand on their own.

For the sweep tools that run a grid of designs without an agent, see
[Design-space exploration](../systems/dse.md) in the flow overview.
