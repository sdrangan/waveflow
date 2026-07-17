---
title: SimObj
parent: Simulation
nav_order: 1
audience: python
api: [SimObj, Simulation]
summary: "The SimObj base: everything in a simulation is a SimObj that registers with the owning Simulation, borrows its single simpy.Environment, and uses the wrapped primitives (self.timeout, self.process, self.event). A runnable toy two-SimObj producer/consumer shows a bare simulation before any interface or HwComponent."
---

# SimObj

## What a `SimObj` is

Everything that participates in a Waveflow simulation is a
[`SimObj`](../../../waveflow/simulation/simobj.py). A `SimObj` registers itself with the owning
[`Simulation`](../../../waveflow/simulation/simulation.py) (which owns the single
`simpy.Environment`), borrows that environment, and exposes the SimPy primitives a process needs so
you never touch SimPy directly:

- `self.timeout(delay)` — wait `delay` time units.
- `self.process(gen)` — start another generator as a concurrent process.
- `self.event()` — create a bare event to wait on / fire.
- `self.now` — the current simulation time.

[Hardware components](../flows/components.md), [interfaces](../interface/), [loggers](./logging.md), and
channels are all `SimObj`s — so the simplest possible simulation is just a couple of `SimObj`s with no
hardware at all.

## Its lifecycle

`Simulation.run_sim()` drives every registered `SimObj` through three phases, in registration order:

1. **`pre_sim()`** — setup / validation before the event loop (bind checks, address ranges, initial state).
2. **`run_proc()`** — the object's optional SimPy generator process is scheduled; an object whose `run_proc` returns `None` is *passive* (it participates only via `pre_sim` / `post_sim`).
3. **`post_sim()`** — collect results, assert invariants, emit reports (after the event loop ends).

If the run raises, `error_cleanup()` is called on every object before the exception propagates, so
files and loggers close cleanly.

A *hardware* `SimObj` adds two synthesis-facing specifics: a regmap-launched component implements
`on_start` — the invocation-style kernel entry the host triggers via `ap_start` — instead of a
free-running `run_proc`, and `@sim_only` marks helpers that exist only for simulation and are excluded
from synthesis extraction. Both are covered where the kernel is generated:
[Component structure](../comp_codegen/structure.md),
[Defining a component: Execution models](../flows/components.md), and the
[Extractor](../comp_codegen/extractor.md).

## Toy: two `SimObj`s interacting

A bare producer/consumer — no interfaces, no `HwComponent`. The `Producer`'s `run_proc` emits a few
items onto a shared SimPy store; the `Consumer`'s `run_proc` pulls them off as they arrive. Both
register with one `Simulation` and run via `run_sim()`:

```python
from dataclasses import dataclass

import simpy

from waveflow.simulation.simobj import ProcessGen, SimObj
from waveflow.simulation.simulation import Simulation


@dataclass
class Producer(SimObj):
    """Emits ``n_items`` integers onto a shared queue, one per time unit."""

    queue: simpy.Store | None = None
    n_items: int = 5

    def run_proc(self) -> ProcessGen[None]:
        for i in range(self.n_items):
            yield self.timeout(1)          # one time unit of "work"
            yield self.queue.put(i)        # hand the item to the consumer
            print(f"[t={self.now:.0f}] {self.name} produced {i}")


@dataclass
class Consumer(SimObj):
    """Pulls ``n_items`` integers off the shared queue as they arrive."""

    queue: simpy.Store | None = None
    n_items: int = 5

    def __post_init__(self) -> None:
        super().__post_init__()
        self.received: list[int] = []

    def run_proc(self) -> ProcessGen[None]:
        for _ in range(self.n_items):
            item = yield self.queue.get()  # blocks until an item is available
            self.received.append(item)
            print(f"[t={self.now:.0f}] {self.name} consumed {item}")


sim = Simulation()
queue = simpy.Store(sim.env)               # a SimPy store shared through the env
producer = Producer(name="producer", sim=sim, queue=queue, n_items=5)
consumer = Consumer(name="consumer", sim=sim, queue=queue, n_items=5)

sim.run_sim()
print("consumer received:", consumer.received)
```

Running it prints the producer and consumer stepping in lockstep, one item per time unit:

```text
[t=1] producer produced 0
[t=1] consumer consumed 0
...
[t=5] producer produced 4
[t=5] consumer consumed 4
consumer received: [0, 1, 2, 3, 4]
```

The point: a complete, running simulation built from nothing but two `SimObj`s — *before* any
interface or `HwComponent` enters the picture. Wiring `SimObj`s together with real transports is
[Interfaces](../interface/); a hardware `SimObj` is a [Hardware Component](../flows/components.md).

## Quick reference

- Every simulation entity is a `SimObj`; construct it with `name=` and `sim=` so it registers with the `Simulation`.
- Override `run_proc` to make an object active (a SimPy generator); leave it to stay passive.
- Use `self.timeout` / `self.process` / `self.event` / `self.now` — not raw SimPy.
- Build a `Simulation()`, construct the `SimObj`s against it, call `run_sim()`.
- The three-phase lifecycle is just above; running a *system* of components is [Running a simulation](./running.md).
