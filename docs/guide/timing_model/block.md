---
title: Sequential bodies
parent: Timing Models
nav_order: 3
audience: python
snippets: run
api: [TimedPhase.timeout, PhaseRecord, StreamIFSlave.get, StreamIFMaster.write]
summary: "A sequential body -- the whole input, then the compute, then the output -- is one span from the input's last beat to the output's first. The body reads and writes as usual and waits the phase with `yield phase.timeout(**features)` between them; the next write closes the span's record. Run on a small module: one 4-word burst, an 11-cycle span, the result delivered at 18. Declaring the span also says the body reads sequentially, so a burst that sat in the input buffer is read at a word a cycle, as the RTL reads it."
---

# Sequential bodies

A **sequential** body needs its **whole input before it can start**: it reads a burst, computes, and
writes the result.  Its timed phase has one span, from the input's **last** beat to the output's
**first** -- poly's `resp_time`, from the end of a command header to the start of its response, is one
(see [the figure](./timed_phases.md#a-phase-on-a-real-trace)).

## The pattern

Declare the span ([Declaring a timed phase](./insertion.md)); in the body, read, compute, and wait the
phase between the read and the write:

```python
from dataclasses import dataclass, field

import numpy as np

from waveflow.hw.clock import Clock
from waveflow.hw.hw_freerun import FreeRunMod
from waveflow.hw.interface import StreamIF, StreamIFMaster, StreamIFSlave
from waveflow.hw.timed_phase import TimedPhase
from waveflow.simulation.simulation import Simulation


@dataclass
class Scale(FreeRunMod):
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
        w = yield from self.s_in.get()              # returns at s_in.last
        y = w * 2                                   # the value -- computed at once in pysim
        yield self.compute_phase.timeout(n=len(w))  # waits compute_time; opens the record
        yield from self.m_out.write(y)              # its first beat closes the record
```

`timeout` returns one SimPy event, so it is `yield`ed like `self.timeout(...)`.  When it is called it
takes the span's start from the start endpoint's most recent transfer, predicts the duration from the
features, and arranges for the **next** transfer on the end endpoint to close the occurrence's record.

## Running it

A driver writes bursts into `s_in`; a sink takes what comes out of `m_out`:

```python
def run(bursts):
    sim = Simulation()
    env = sim.env
    dut = Scale(name="scale", sim=sim)
    drv = StreamIFMaster(name="drv", sim=sim, bitwidth=64)
    snk = StreamIFSlave(name="snk", sim=sim, bitwidth=64, queue_size=64)
    for name, master, slave in (("a", drv, dut.s_in), ("b", dut.m_out, snk)):
        link = StreamIF(name=name, sim=sim, bitwidth=64, clk=Clock(freq=1.0), depth=16)
        link.bind("master", master)
        link.bind("slave", slave)
    out = []

    def driver():
        for b in bursts:
            yield from drv.write(np.asarray(b, dtype=np.uint64))

    def sink():
        for _ in bursts:
            w = yield from snk.get()
            out.append((env.now, [int(v) for v in w]))

    env.process(driver())
    env.process(sink())
    env.process(dut._run_iter_forever())
    env.run(until=200)
    return dut, out


dut, out = run([[1, 2, 3, 4]])
print(out)
r = dut.compute_phase.records[0]
print(r.start, r.end, r.predicted, r.actual)
```

```text
[(18.0, [2, 4, 6, 8])]
('scale_s_in', 0, 'last', 4.0) ('scale_m_out', 0, 'first', 15.0) 11.0 11.0
```

The four words land at 4 (`s_in.last`); `compute_time = 3 + 2·4 = 11`; the result's first beat is at
15 (`m_out.first`), and the four result words are delivered at 18.  The record pairs the span's two
events -- `(endpoint, transfer index, edge, time)` -- with its features and the predicted and actual
durations: pysim's half of a [calibration](./fit.md).

## A burst that waited is read a word a cycle

A second burst arrives while the module is still busy with the first, and waits in the input buffer:

```python
dut, out = run([[1, 2, 3, 4], [5, 6, 7, 8]])
print([t for t, _w in out])
print(dut.compute_phase.records[1].start)
```

```text
[18.0, 35.0]
('scale_s_in', 1, 'last', 21.0)
```

The module asks for it at 18; it was delivered at 8, but the hardware still reads it one word a
cycle, so its last beat is at 21, not 18 -- and the second span starts there.  Declaring a span that
starts at `s_in.last` says the body reads sequentially, so its reads of a waiting burst are charged
their beats.  (Reads on an endpoint no phase names are left as they were: there the simulation does
not know the body's shape.)

## What the phase does not charge

The span is only the compute.  The read and the write are timed by their own `yield from`s, and a
stall in either -- a full downstream queue, a late producer -- lengthens the firing on its own, as
[Declaring a timed phase](./insertion.md#the-delay-is-additional-not-end-to-end) explains.

## Blocks from a stream of blocks

A block that arrives over a [stream of blocks](../interface/primitive/sob.md) (`acquire_read` /
`release_read`) has the same sequential shape, but stream-of-blocks endpoints do not have `first` /
`last` events yet, so such a compute cannot be declared as a phase today.  It is on the plan
(`plans/pipelined_ops.md`, the deferred endpoint kinds).

## See also

- [What a timed phase is](./timed_phases.md) -- spans and events, on a real trace.
- [Pipelined bodies](./streaming.md) -- the other shape: compute while the data arrives.
- [The form of a span's model](./loops.md) -- `compute_time` for a loop: `latency + ii·(m − 1)`.
- [Calibrating a timed phase](./fit.md) -- fitting `compute_time` from an RTL trace.
