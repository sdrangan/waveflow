---
title: What a timed phase is
parent: Timing Models
nav_order: 1
audience: python
api: [TimedPhase, TimingEvent]
summary: "A module's timing is stated as timed phases: spans between events at its own ports -- the first or last beat of a transfer -- whose durations are a function of features known before the span starts, learned by a model. Shown on a real RTL trace of the polynomial kernel: a 4-cycle response, a 34-cycle latency and a 7-cycle fill. Everything a calibration needs -- what to probe, what to log, how to pair pysim with RTL -- follows from the declaration."
---

# What a timed phase is

A Waveflow simulation is [loosely timed](./models.md): it does not step every clock edge.  A transfer
on a port -- a burst of words on a stream -- is one simulated transaction, and what the simulation
knows about its timing is two moments: its **first beat** and its **last beat**.  Those two moments
are the only things a module's timing can be stated against.

So a module states its timing as **timed phases**.  A phase names one or more **spans**, each the time
between two of those events on the module's own ports, and the **features** the span depends on.  A
**timing model** -- one per span -- predicts the span's duration from the features.

## A phase, on a real trace

![One poly command at RTL: the header and samples arriving on s_in, the response and results leaving on
m_out, and three spans between their events: resp_time = 4, latency = 34, fill_time = 7](figures/timed_phase.svg)

This is the [polynomial kernel](../../examples/stream_inband/index.md) at RTL, one 8-sample command, beat
by beat on its two ports (a cycle-accurate co-simulation; each box is one word accepted).  Poly declares
two phases, and its three spans are drawn where the trace puts them:

- **`resp_time`** -- from the **last** beat of the command header on `s_in` to the **first** beat of the
  response on `m_out`: 4 cycles.  The kernel decodes the header and answers.
- **`latency`** -- from the **first** beat of the samples on `s_in` to the **first** beat of the results
  on `m_out`: 34 cycles, the depth of the evaluation pipeline.
- **`fill_time`** -- from the **first** to the **last** beat of the results: 7 cycles for 8 words, one
  word a cycle.

The picture also shows what a phase does *not* have to model.  The samples arrive with a gap -- the
kernel was still handling the header -- and the results leave long after the last sample came in.
Neither is a separate number: the gap falls inside `latency`, which is measured from the samples'
first beat, and the transfers themselves are timed by the simulation already.

## The two rules

A span can be calibrated only if two things hold:

1. **Its duration is a deterministic function of features known before its first event.**  Poly's
   `fill_time` depends on the number of words, which the header announced before the first result
   word; its `latency` on nothing at all.  The model learns that function: its *targets* are the
   observable durations, and its parameters -- a latency, a rate -- stay inside it.  They never
   appear in the module's code.
2. **Every event is measurable.**  An event is an endpoint's first or last beat -- a handshake any RTL
   trace shows -- and the simulation's own transfer primitive logs the same event.  On a
   stream-of-blocks endpoint a transfer is one **block**: `first` is its acquire (the task's first
   access to it), `last` its release (the lock's release pulse).  A task inside a composite kernel --
   whose endpoints are the kernel's internal channels, not its ports -- is measured the same way, its
   events read by name from the kernel top's trace manifest at the kernel's scope
   ([the systolic core](../dsp/systolic/timing.md#where-the-timing-is-measured) is the example).

A duration is deterministic only while the environment does not hold the module up inside the span (a
full output queue, an input that is late), so every measurement also says whether it was held -- a
held measurement tests a prediction but is not used to fit one.

## Two shapes

Most module bodies are one of two shapes, and each is one kind of phase:

- **Sequential** -- the whole input arrives, then the compute, then the output: one span, from the
  input's **last** beat to the output's **first** (poly's `resp_time`).  See
  [Sequential bodies](./block.md).
- **Pipelined** -- a loop that reads a word, computes, writes a word: the output starts a **latency**
  after the input's **first** beat and then fills at the loop's rate -- two spans (poly's `latency` and
  `fill_time`).  See [Pipelined bodies](./streaming.md).

The difference between them is only *which event the span starts at*.

## What follows from a declaration

A phase is declared once, beside the module's endpoints ([Declaring a timed phase](./insertion.md)).
Everything a calibration needs follows from it, with nothing written by hand:

- **what to observe** -- every endpoint a phase names, which is a port of the generated kernel;
- **what pysim logs** -- each of those endpoints' transfers, and each occurrence of each span with its
  features;
- **what the RTL shows** -- the same transfers, read off a trace of those ports;
- **how they pair** -- pysim says which transfers each span started and ended on, and with which
  features; the RTL supplies the times.

[Calibrating a timed phase](./fit.md) is those steps.  The figure above is drawn from the very trace
poly's phases were fitted from.

## See also

- [LT vs CT models](./models.md) -- why a transfer is one event and not one per cycle.
- [Declaring a timed phase](./insertion.md) -- the API: `TimedPhase`, `add_timing_phase`.
- [The form of a span's model](./loops.md) -- what a span's model is, for a pipelined loop.
- `plans/pipelined_ops.md` -- the design and its record.
