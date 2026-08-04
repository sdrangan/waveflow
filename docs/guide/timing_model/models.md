---
title: LT vs CT models
parent: Timing Models
nav_order: 1
audience: python
api: [Clock, SimObj.timeout]
summary: "Loosely-timed (LT) vs cycle-timed (CT) simulation. Waveflow models timing loosely — one timed event per transaction, not per clock edge — which is what makes a whole-system Python simulation fast, and is the same choice established computer-architecture simulators make."
---

# Loosely-timed vs cycle-timed

A timing model can be written at two very different levels of detail.

## Cycle-timed (CT)

A **cycle-timed** (or cycle-accurate) model advances one clock edge at a time and resolves the
state of every signal on every cycle. It is the most faithful — it can reproduce pipeline stalls,
arbitration, and back-pressure to the exact cycle — but it is expensive: simulating a 1024-word
burst means stepping the simulator 1024+ times, and a whole system of such blocks multiplies out.
RTL simulation (and Vitis HLS **cosim**) is cycle-timed; it is where you go for ground truth, not
for fast design-space exploration.

## Loosely-timed (LT)

A **loosely-timed** model does not step every cycle. It represents a unit of work as a single
**timed transaction**: compute *how long* the work takes (in cycles, converted to seconds through
the [`Clock`](../../../waveflow/hw/clock.py)), then advance simulated time by that amount in one
jump with `yield self.timeout(...)`. The functional result is computed all at once; only the
*timestamps* are modeled. A 1024-word burst is **one** event, not 1024.

This is exactly the discrete-event style Waveflow's [simulation](../sim/) is built on: time only
moves when something happens, so the cost of a simulation scales with the number of transactions,
not the number of cycles.

## Why Waveflow is LT

Waveflow's milestone is to **simulate a whole system in Python** before any C++ is generated —
wire components through interfaces, run them, and check that the data flows and the results are
correct. That is a design-space-exploration job: you run it constantly, over many configurations,
and you need it to be fast and to compose across dozens of blocks. An LT model delivers that. The
trade is precision: an LT model predicts timing within a calibrated error band rather than to the
exact cycle. When you need the exact cycle count, you fall back to cosim — and you can *calibrate*
the LT model against it (see [Fitting a timing model](../timing_model/fit.md)).

The flows in this section — [block](./block.md) and [streaming](./streaming.md) — are LT models. They
differ only in how much load/compute/store overlap their timeline exposes, not in whether they step
every cycle (neither does). Double-buffering is the same idea one level up, built by composing these
over a [stream of blocks](../interface/sob.md).

## Not a new idea

Loosely-timed, transaction-level modeling is the established approach for fast architectural
simulation. SystemC TLM-2.0 defines an explicit *loosely-timed* coding style for exactly this
purpose, and widely-used computer-architecture simulators (gem5 in its faster CPU modes, and
event-driven SoC virtual platforms) make the same bet: model transactions and their latencies, not
every clock edge, so a full system stays tractable. Waveflow's contribution is to express that
model in the *same* Python that defines the functional behavior and that feeds synthesis, so one
source of truth drives simulation, timing, and codegen.

## See also

- [Simulation](../sim/) — the SimPy discrete-event runtime an LT model runs on.
- [Block processing](./block.md) — the first and simplest LT timing model.
- [Timing Analysis Tools](../timing/) — including the cosim path that produces the cycle-accurate ground truth an LT model is calibrated against.
