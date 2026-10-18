---
title: Design-space exploration
parent: Flow Overview
nav_order: 8
audience: python
api: [ParamGrid, SweepRunner, sweep_cli, ResourceModel, HwParam]
summary: "The outer loop of the flow: choosing among designs that work. A design's build parameters (HwParam) are the axes; a sweep runs the flow at every point of a grid and keeps what each step measured (ParamGrid, SweepRunner); calibrated timing and resource models price a point without synthesizing it, each prediction carrying a confidence; and a few syntheses check the predictions. What is built today -- sweeps, resource models, calibrated timing, per-device synthesis -- and what is not yet: an exploration loop as a framework tool. The mimo_cg study is the worked case."
---

# Design-space exploration

The steps before this one take **one design** from Python to verified, measured RTL. Exploration is the
loop around them: many designs, the same flow, and a choice. It works because every step before it is
automatic, incremental, and measured per module.

## The axes are build parameters

A design's choices -- a word width, a number of lanes, a queue depth, a pipeline split -- are its
**build parameters** ([`HwParam`](../flows/parametrization.md)): each value is a different hardware
module, with its own identity, its own RTL and its own calibration. Changing one re-runs only what
depends on it: change markov's generator's draw interval and only the generator is re-synthesized,
re-run and calibrated, with the system that holds it.

## A sweep runs the flow at every point

A [sweep](../build/sweep.md) is the flow at every point of a grid: `ParamGrid` is the points,
`SweepRunner` runs each through the steps and keeps what they measured, resuming where it stopped and
isolating a point that fails. The example supplies its axes, its DAG and its platform; the runner owns
the rest.

## Models price a point without building it

Synthesis is the slow step, so a large space is priced by **models** and checked by a few builds:

- **timing** -- the calibrated [timed phases](calibration.md) give a system's cycle count from its
  modules' records, at pysim's speed;
- **resources** -- a [resource model](../resource_model/index.md) predicts a module's utilization from
  measurements, structure or a regression over a corpus, and composes a hierarchy as each module's own
  cost plus its children's;
- **confidence** -- every prediction says how much to believe it, and a composed estimate reports its
  weakest link ([Model calibration](../calib/index.md)).

A per-device synthesis makes the checks cheap: each device's number is its own, so a change re-measures
only what changed ([Toward the board](../flows/bitstream_ipi.md)).

## What exists, and what does not yet

Built: parameterized modules, sweeps, calibrated timing composition, resource models with confidence,
per-device synthesis. Not yet a framework tool: the exploration loop itself -- choosing the next points
from the models, and deciding which to confirm by building. Today an example writes that loop.

**The worked case is [mimo_cg](../../examples/mimo_cg/index.md)**: a conjugate-gradient detector whose
study swept accuracy against cost, priced points with calibrated cost models, and checked its exploration
against a 1,440-build brute force.

This is the last step of the flow. Back to the [Flow Overview](index.md).
