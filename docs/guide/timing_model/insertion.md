---
title: Declaring a timed phase
parent: Timing Models
nav_order: 2
audience: python
snippets: run
api: [TimedPhase, TimingEvent, HwModule.add_timing_phase, TimedPhase.predict, LinearTiming]
summary: "How a module declares its timing: build a TimedPhase from spans between its own endpoints' events (ep.first / ep.last), the features the durations depend on, and seed numbers; register it with add_timing_phase, like an endpoint. The phase's model predicts each span in cycles with no simulator. The rules: a span uses the module's own endpoints, an event names an operation not a role or a message, features are checked by name. The phase charges only the module's own time; the transfers time themselves, and a stall lengthens a firing on its own."
---

# Declaring a timed phase

A module declares its [timed phases](./timed_phases.md) once, in `__post_init__`, beside its endpoints.
This page is the declaration; the body's side is [Sequential bodies](./block.md) and
[Pipelined bodies](./streaming.md).

## The declaration

Every endpoint provides its two events, `ep.first` and `ep.last` -- the first and last beat of each of
its transfers.  A phase names its **spans** as pairs of those events, the **features** the durations
depend on, and **seed** numbers -- what it predicts before it is calibrated.  It is registered like an
endpoint: construct, assign, register.

```python
from dataclasses import dataclass, field

from waveflow.hw.clock import Clock
from waveflow.hw.hw_freerun import FreeRunMod
from waveflow.hw.interface import StreamIFMaster, StreamIFSlave
from waveflow.hw.timed_phase import TimedPhase
from waveflow.simulation.simulation import Simulation


@dataclass
class Scale(FreeRunMod):
    """Reads a burst of n words, computes, writes the result: compute_time = 3 + 2 n cycles."""

    clk: Clock = field(default_factory=lambda: Clock(freq=1.0))   # 1 Hz: times read as cycles

    def __post_init__(self):
        super().__post_init__()
        self.s_in = StreamIFSlave(name=f"{self.name}_s_in", sim=self.sim, bitwidth=64)
        self.m_out = StreamIFMaster(name=f"{self.name}_m_out", sim=self.sim, bitwidth=64)
        for ep in (self.s_in, self.m_out):
            self.add_endpoint(ep)
        self.compute_phase = TimedPhase(
            "compute", spans={"compute_time": (self.s_in.last, self.m_out.first)},
            features=("n",), seed={"compute_time": {"intercept": 3.0, "n": 2.0}})
        self.add_timing_phase(self.compute_phase)

    def run_iter(self):
        w = yield from self.s_in.get()
        yield self.compute_phase.timeout(n=len(w))
        yield from self.m_out.write(w * 2)


m = Scale(name="scale", sim=Simulation())
print(m.compute_phase.spans)
print(m.compute_phase.predict(n=4))
```

```text
{'compute_time': (scale_s_in.last, scale_m_out.first)}
{'compute_time': 11.0}
```

- **A phase and its model are one object.**  `phase.predict(**features)` is plain arithmetic, in
  **cycles** of the module's clock, and needs no simulator: the same model a calibration fits.  Each
  span gets its own model -- `LinearTiming` by default, `intercept + sum(coefficient · feature)` -- so
  a phase with two spans predicts two named durations.
- **The numbers are not in the code.**  The seed is a starting point; a calibration files fitted
  parameters in the phase's `calib_dir` (`<span>.json`), and the phase loads them when they are there.
  The declaration says *what* is timed and the law's *shape*; the store says how well it is known.
- **Registering binds the phase to its module** -- through it the phase reaches the clock -- and turns
  on what its events need: a transfer log on each endpoint it names.  `add_timing_phase` returns
  `None`, so there is one way to write it, and phases share a loop as endpoints do.

## The rules a declaration is held to

**Features are checked by name.**  A prediction or a wait with a missing or unexpected feature fails
at the call, instead of fitting the wrong thing:

```python
try:
    m.compute_phase.predict(words=4)
except TypeError as e:
    print(e)
```

```text
phase 'compute' declares features ['n']; got ['words'] (missing ['n'], unexpected ['words'])
```

**A span uses only the module's own endpoints.**  A span between two modules belongs to the composite
that holds both, declared there; leaves calibrate their own phases.  A point inside an HLS body cannot
be an event -- if a phase cannot be stated between port events, the module is split.

```python
other = Scale(name="other", sim=Simulation())
try:
    m.add_timing_phase(TimedPhase("x", spans={"t": (m.s_in.last, other.m_out.first)}))
except ValueError as e:
    print(str(e).split(".  ")[0])
```

```text
phase 'x' on 'scale': event other_m_out.first is on an endpoint that is not the module's own
```

**An event names an operation on an endpoint, never a role and never a message.**  "Load" and "store"
are roles a body gives transfers, not properties of an endpoint, so there is no `s_in.load.first`.
And `s_in.last` is "the last beat of *a* transfer on `s_in`": which transfer is decided when the body
runs, so one declaration serves every message -- poly's `s_in` carries both its command headers and its
samples, and two phases start on it.

## The delay is *additional*, not end-to-end

A phase times **only the module's own work** -- the compute between the events.  The transfers time
themselves:

- `s_in.get()` and `m_out.write()` take as long as the transfer takes, charged by their `yield from`;
- if a transfer **stalls** -- a downstream FIFO is full, a producer is behind -- that `yield from`
  simply takes longer, and the firing's end-to-end time **grows on its own**.

So a phase calibrated in isolation still predicts the contended system: congestion is the
simulation's to model, and the phase owns only the module's own delay.  The same split is what makes a
measurement honest: an occurrence the environment held up is marked, and kept out of a fit.

## See also

- [What a timed phase is](./timed_phases.md) -- spans, events, features, on a real trace.
- [Sequential bodies](./block.md) / [Pipelined bodies](./streaming.md) -- waiting the phase in the body.
- [The form of a span's model](./loops.md) -- what `LinearTiming` is, for a pipelined loop.
- [Calibrating a timed phase](./fit.md) -- where the fitted numbers come from.
