---
title: Simulation
parent: Guide
nav_order: 6
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

## The three-phase lifecycle (at a glance)

`Simulation.run_sim()` drives every registered object through three phases, in registration order:

1. **`pre_sim()`** — setup / validation before the event loop (bind checks, address ranges, initial state).
2. **`run_proc()`** — each object's optional SimPy generator process is scheduled (objects returning `None` are *passive* — they participate only via `pre_sim` / `post_sim`).
3. **`post_sim()`** — collect results, assert invariants, emit reports (after the event loop ends).

If the run raises, `error_cleanup()` is called on every object before the exception propagates (so
files/loggers close). The **per-object** meaning of these hooks — and `on_start` for regmap-launched
kernels — is on the [component Lifecycle](../components/lifecycle.md) page; this section is the
*system* view: how the hooks are driven across all objects at once.

## In this section

- [Running a simulation](./running.md) — instantiate components, wire them with `Interface` objects, build a `Simulation`, and call `run_sim()`.
- [Timing model](./timing.md) — `Clock` and how components / interfaces express cycle latency (the forward model that produces the timeline).
- [Logging](./logging.md) — the `Logger` `SimObj`: recording timestamped events to a CSV for inspection and timing analysis.

## See also

- [Interfaces](../interface/) — the transactional connections components are wired through.
- [Hardware Components](../components/) — declaring the components (ports, `HwParam`, lifecycle) that a simulation runs.
- [Timing Analysis Tools](../timing/) — *analyzing* the timeline a simulation produces (this section is the model that produces it).
- [Build System](../build/python.md) — running a simulation as a step in a `BuildDag`.

> The simulation models a design in Python. Turning that design into synthesizable C++ — the
> generated kernel structure and the hand-written kernel bodies — is the codegen arc:
> [Component Code Generation](../comp_codegen/) and [Custom Hooks](../custom_hooks/).
