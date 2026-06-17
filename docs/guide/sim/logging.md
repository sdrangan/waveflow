---
title: Logging
parent: Simulation
nav_order: 3
audience: python
api: [Logger, Logger.log, Logger.get_tv, NullLogger]
summary: "Record timestamped events during a simulation: Logger is a SimObj that writes a flushed CSV (a leading time column from env.now plus your declared fields); log(**fields) records a row, get_tv(field) reads back (times, values). NullLogger is the no-op default."
---

# Logging

[`Logger`](../../../waveflow/simulation/logger.py) is a [`SimObj`](../../../waveflow/simulation/simobj.py)
that writes a **timestamped CSV** as the simulation runs — the standard way to capture *when* events
happened for inspection and [timing analysis](../timing/). Because it is a `SimObj`, it shares the
environment and closes its file automatically in `post_sim()` (and on error).

## Creating a logger

You declare the **field columns** up front; a leading `time` column is added automatically:

```python
from waveflow.simulation.logger import Logger

logger = Logger(name="poly_log", sim=sim,
                file_path="results/sim_log.csv",
                fields=["event", "job"])
```

`Logger` is keyword-only (`name`, `sim`, `file_path`, `fields`). On construction it opens the file
and writes the header `time,event,job`.

## Recording events

Call `log(**kwargs)` from inside any `run_proc` (or a component method). Only declared fields are
accepted; unspecified ones are written blank. The row is stamped with the current simulation time
(`env.now`) and written through a SimPy resource, so concurrent callers never interleave:

```python
def run_proc(self) -> ProcessGen[None]:
    self.logger.log(event="start", job=self.job_id)   # -> 1.0e-7,start,3
    # ... do work ...
    self.logger.log(event="done")                     # -> 2.5e-7,done,   (job blank)
    yield self.timeout(0)
```

Each entry is **flushed immediately**, so the CSV is inspectable even if the run later raises.
Logging is marked `@sim_only` — it is simulation instrumentation and is excluded from synthesis
extraction.

## Reading results back

After [`run_sim()`](./running.md), pull a `(times, values)` pair for one field. Only rows where that
field was explicitly logged are returned; values are cast to `float` when possible, else left as
strings:

```python
times, events = logger.get_tv("event")   # times: list[float], events: list
```

This is the bridge to [timing analysis](../timing/) — e.g. the gap between a `start` event and the
matching `done` is a measured latency.

## `NullLogger` — the no-op default

[`NullLogger`](../../../waveflow/simulation/logger.py) is a drop-in whose `log(...)` discards
everything. Components default their `logger` field to a `NullLogger`, so call sites can always write
`self.logger.log(...)` without an `if self.logger:` guard; you pass a real `Logger` only when you
want a trace.

```python
from waveflow.simulation.logger import NullLogger

logger: Logger | NullLogger = field(default_factory=NullLogger)   # on a component
```

## In a build step

[`examples/stream_inband/poly_build.py`](../../../examples/stream_inband/poly_build.py)'s `PySimStep`
constructs a `Logger(name="poly_log", sim=sim, file_path=log_path, fields=["event", "job"])`, hands
it to the accelerator, runs the simulation, and leaves the CSV under `results/` for a later step to
parse — see [Build System](../build/python.md) for running a simulation inside a `BuildDag`.

## See also

- [Running a simulation](./running.md) — where the `Logger` is constructed and `run_sim()` is called.
- [Timing model](./timing.md) — the `self.now` timestamps each log row carries.
- [Timing Analysis Tools](../timing/) — analyzing the recorded timeline.
- [Build System](../build/python.md) — `PySimStep` and producing logs as build artifacts.
