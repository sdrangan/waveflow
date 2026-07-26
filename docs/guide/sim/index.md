---
title: Simulation
parent: Guide
nav_order: 4
has_children: true
audience: python
api: [Simulation, SimObj, Clock, Logger]
summary: "Run a whole system in Python: a SimPy discrete-event simulation that wires components through Interface objects and drives the pre_sim / run_proc / post_sim lifecycle — the milestone of validating a design before any C++."
---

# Simulation

A Waveflow design is, first, a **Python program you can run**. Before any C++ is generated, you
assemble your components, wire them together with [interfaces](../interface/), and **simulate the
whole system** — checking that the data flows, the protocol handshakes, and the results are correct.
That is the milestone: *simulate a whole system in Python.*

## Discrete-event simulation (PySim)

The simulation is **discrete-event** (DES): nothing advances on a fixed clock tick — instead the
runtime jumps from one scheduled event to the next, so time only moves when something happens. This
is what makes a transaction-level model fast: a 1024-word burst is one timed event, not 1024 clock
steps.

Waveflow builds this on [**SimPy**](https://simpy.readthedocs.io/). The
[`Simulation`](../../../waveflow/simulation/simulation.py) object owns a single `simpy.Environment`
and the list of participating objects; every simulation entity is a
[`SimObj`](../../../waveflow/simulation/simobj.py) that registers itself with that `Simulation` and
borrows its environment. You rarely touch SimPy directly — `SimObj` wraps the primitives you need
(`self.timeout(...)`, `self.process(...)`, `self.event()`).

## The three-phase lifecycle

`Simulation.run_sim()` drives every registered `SimObj` through `pre_sim` → `run_proc` → `post_sim`,
in registration order (`error_cleanup()` runs on every object if the run raises, so files and loggers
close). That per-object lifecycle — and the `on_start` / `@sim_only` component specifics — is on the
[SimObj](./simobj.md#its-lifecycle) page; this is the *system* view: how the hooks are driven across
all objects at once.

## In this section

- [SimObj](./simobj.md) — the base object every simulation entity is, its three-phase lifecycle, and a toy two-`SimObj` producer/consumer simulation.
- [Process generators](./procgen.md) — SimPy's `yield`-based concurrency for newcomers: events, pausing, parallel spawn, return values, and `ProcessGen[T]` hints.
- [Running a simulation](./running.md) — instantiate components, wire them with `Interface` objects, build a `Simulation`, and call `run_sim()`.
- [Stream drivers and sinks](./stream_tb.md) — the two reusable testbench participants (`StreamDriver` / `StreamSink`): schema-blind stimulus and capture, and the pysim twins of the XSI BFM models.
- [Timing model](./timing.md) — `Clock` and how components / interfaces express cycle latency (the forward model that produces the timeline).
- [Logging](./logging.md) — the `Logger` `SimObj`: recording timestamped events to a CSV for inspection and timing analysis.

## See also

- [Interfaces](../interface/) — the transactional connections components are wired through.
- [Hardware Modules](../flows/modules.md) — declaring the components (ports, `HwParam`) that a simulation runs.
- [Timing Analysis Tools](../timing/) — *analyzing* the timeline a simulation produces (this section is the model that produces it).
- [Build System](../build/python.md) — running a simulation as a step in a `BuildDag`.

> The simulation models a design in Python. Turning that design into synthesizable C++ — the
> generated kernel structure and the hand-written kernel bodies — is the codegen arc:
> [Component Code Generation](../comp_codegen/) and [Custom Hooks](../custom_hooks/).
