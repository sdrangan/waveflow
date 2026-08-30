---
title: Running a simulation
parent: Simulation
nav_order: 3
audience: python
api: [Simulation, Simulation.run_sim, SimObj, Clock, StreamIF, DirectMMIF, Interface.bind]
summary: "Assemble and run a system in Python: instantiate components against one Simulation, wire their ports with Interface objects (.bind), and call run_sim() to drive pre_sim / run_proc / post_sim. Results are read off the objects afterward."
---

# Running a simulation

Running a simulation is four steps: **(1)** create a `Simulation`, **(2)** instantiate your
components against it, **(3)** wire their ports together with `Interface` objects, and **(4)** call
`run_sim()`. Results are read off the component objects after the run returns.

## The shape

```python
from waveflow.hw.clock import Clock
from waveflow.simulation.simulation import Simulation

sim = Simulation()                       # owns the SimPy environment
clk = Clock(freq=100e6)                  # 100 MHz timing domain

producer = Producer(name="prod", sim=sim)     # each SimObj registers itself with sim
consumer = Consumer(name="cons", sim=sim)

link = StreamIF(sim=sim, clk=clk)             # an Interface object carries the timing model
link.bind("master", producer.ep)              # wire endpoints by role
link.bind("slave",  consumer.ep)

sim.run_sim()                                 # pre_sim → run_proc → post_sim, all objects
```

- Every [`SimObj`](../../../waveflow/simulation/simobj.py) takes `sim=sim` and **registers itself**
  on construction (`Simulation.add_obj` is called for you) — you never build the object list by hand.
- [`Simulation.run_sim()`](../../../waveflow/simulation/simulation.py) runs every registered object's
  `pre_sim()`, schedules each non-`None` `run_proc()` as a SimPy process, advances the event loop,
  then runs every `post_sim()`. (For what those hooks mean per object, see
  [Lifecycle](./simobj.md#its-lifecycle).)

## Wiring: `Interface` objects, not a `connect()` call

There is **no framework `connect()`**. Components expose typed **endpoints** (a `StreamIFMaster`, an
`MMIFSlave`, …); you connect two endpoints by creating an [`Interface`](../interface/) object and
calling **`iface.bind(role, endpoint)`** — `role` is `"master"` / `"slave"` (or `in_0` / `out_0`
for a crossbar). The interface object — not the endpoint — carries the clock and the latency model.

```python
from waveflow.hw.interface import StreamIF
from waveflow.hw.memif import DirectMMIF

in_stream  = StreamIF(sim=sim, clk=clk)
out_stream = StreamIF(sim=sim, clk=clk)
lite_link  = DirectMMIF(sim=sim, clk=clk, byte_addressable=True)

in_stream.bind ("master", tb.m_in);    in_stream.bind ("slave", accel.s_in)
out_stream.bind("master", accel.m_out); out_stream.bind("slave", tb.s_out)
lite_link.bind ("master", tb.m_lite);  lite_link.bind ("slave", accel.s_lite)
```

The endpoint classes and their `write` / `read` / `get` calls are documented under
[Interfaces](../interface/) — [streams](../interface/primitive/stream.md), [memory-mapped](../interface/primitive/aximm.md),
[register maps](../interface/primitive/regmap.md). This page is only about *assembling and running* the system.

## A minimal complete example

A producer streams three bursts to a consumer over one `StreamIF`:

```python
from dataclasses import dataclass
import numpy as np

from waveflow.hw.clock import Clock
from waveflow.hw.interface import StreamIF, StreamIFMaster, StreamIFSlave, Words
from waveflow.simulation.simobj import ProcessGen, SimObj
from waveflow.simulation.simulation import Simulation


@dataclass
class Producer(SimObj):
    def __post_init__(self) -> None:
        super().__post_init__()
        self.ep = StreamIFMaster(sim=self.sim, bitwidth=32)

    def run_proc(self) -> ProcessGen[None]:
        for i in range(3):
            yield self.process(self.ep.write(np.array([i, i + 1], dtype=np.uint32)))


@dataclass
class Consumer(SimObj):
    def __post_init__(self) -> None:
        super().__post_init__()
        self.received: list[np.ndarray] = []
        self.ep = StreamIFSlave(sim=self.sim, bitwidth=32, rx_proc=self.on_rx)

    def on_rx(self, words: Words) -> ProcessGen:
        self.received.append(words.copy())
        yield self.env.timeout(0)

    def run_proc(self) -> ProcessGen[None]:
        yield from self.ep.run_proc()        # start the slave receive loop

    def post_sim(self) -> None:
        assert len(self.received) == 3       # results checked after the run


sim = Simulation()
clk = Clock(freq=100e6)
producer = Producer(name="prod", sim=sim)
consumer = Consumer(name="cons", sim=sim)

link = StreamIF(sim=sim, clk=clk)
link.bind("master", producer.ep)
link.bind("slave",  consumer.ep)

sim.run_sim()
print(consumer.received)   # [array([0,1]), array([1,2]), array([2,3])]
```

A passive component (one whose `run_proc` returns `None`) still participates through `pre_sim` /
`post_sim`; a slave that needs an active receive loop delegates to `ep.run_proc()` as `Consumer`
does. Reading **results** is a `post_sim()` job (or just reading attributes after `run_sim()`
returns, as the producer/consumer does here).

## Worked example: the poly accelerator

[`examples/stream_inband/poly.py`](../../../examples/stream_inband/poly.py) wires a testbench
(`PolyTB`) to an accelerator (`PolyAccel`) over two `StreamIF`s and one `DirectMMIF` (the
AXI-Lite control link) — exactly the `bind` pattern above, collected into an example-local
`connect(sim, tb, accel, clk)` helper (it is a convenience in that file, *not* a framework API). The
run itself lives in [`poly_build.py`](../../../examples/stream_inband/poly_build.py)'s `PySimStep`:
it builds the `Simulation`, a `Clock`, a [`Logger`](./logging.md), the accelerator and testbench,
calls `connect(...)`, then `sim.run_sim()` — and reads `tb.resp_hdr` / `tb.samp_out` and the regmap
status off the objects afterward. Running a simulation inside a `BuildStep` like this is covered in
[Build System](../build/python.md).

## See also

- [Interfaces](../interface/) — the endpoint classes and their transaction calls.
- [Lifecycle](./simobj.md#its-lifecycle) — the per-object `pre_sim` / `run_proc` / `post_sim` hooks `run_sim` drives.
- [Timing model](./timing.md) — how the `Clock` and interface latencies shape the simulated timeline.
- [Logging](./logging.md) — recording events during the run.
