---
title: Stream Interfaces
parent: Interfaces
nav_order: 2
audience: python
api: [StreamIF, StreamIFMaster, StreamIFSlave, CrossBarIF, CrossBarIFInput, CrossBarIFOutput, SimObj, Simulation]
summary: "Unidirectional stream interfaces in the SimPy model — point-to-point StreamIF (write/rx_proc), the CrossBarIF switching fabric, and pipelined get/write timing, with a runnable two-SimObj producer→consumer toy."
---

# Stream Interfaces

Stream interfaces model **unidirectional data bursts** from one component to another. They correspond to AXI4-Stream and Vitis HLS stream bus protocols. Two interface classes are available: `StreamIF` for point-to-point connections and `CrossBarIF` for n-input × m-output switching fabrics.

## Point-to-point: StreamIF

`StreamIF` connects one master endpoint to one slave endpoint. The master calls `write(words)` to push a burst; the slave receives it via an `rx_proc` callback after the modelled latency.

### Classes

| Class | Role | Key parameters |
|---|---|---|
| `StreamIF` | Interface | `clk`, `bitwidth`, `latency_init` |
| `StreamIFMaster` | Master endpoint | `bitwidth` |
| `StreamIFSlave` | Slave endpoint | `bitwidth`, `rx_proc`, `queue_size` |

### Latency model

```
transfer_time = (latency_init + nwords) / clk.freq   [seconds]
```

- `latency_init` — fixed cycles for wire delay, arbitration, etc.
- `nwords` — one additional cycle per word in the burst (one beat per clock)

### Pipelined processing

Use `get_pipelined` / `write_pipelined` when the component processes data as it streams through (rather than buffering the full burst first). These methods carry pipeline timing explicitly so the simulation reflects the latency and throughput of synthesized hardware.

**`StreamIFSlave.get_pipelined(schema_type, count=N)`** returns `(data, tstart)`:

- `data` — deserialized burst, identical to `get(schema_type, count=N)`
- `tstart` — SimPy time when the **first** word of the burst arrived

`tstart` is back-calculated from the completion time of the burst:

```
tstart = env.now - (nwords_transferred - 1) * clk.period
```

This is exact for a back-pressure-free, II=1 input stream.

**`StreamIFMaster.write_pipelined(data, t_out_start, ii=1)`** waits until `t_out_start` before beginning the write:

```
t_out_start = tstart + proc_latency * clk.period
```

`ii` documents the output initiation interval (informational; reserved for future per-word output pacing).

**Skeleton for a pipelined `evaluate`:**

```python
@synthesizable
def evaluate(self, cmd_hdr, s_in, m_out):
    resp_hdr = PolyRespHdr()
    resp_hdr.tx_id = cmd_hdr.tx_id
    yield from m_out.write(resp_hdr)

    samp_in, tstart = yield from s_in.get_pipelined(Float32, count=cmd_hdr.nsamp)

    # ... compute output y from samp_in ...

    t_out_start = tstart + self.proc_latency * self.clk.period
    yield from m_out.write_pipelined(
        array(Float32, y), t_out_start, ii=self.proc_ii
    )
    if len(samp_in) != cmd_hdr.nsamp:
        return PolyError.WRONG_NSAMP
    return PolyError.NO_ERROR
```

Set `proc_ii` and `proc_latency` on the component to match values reported by HLS synthesis for the `evaluate` loop.

### Example: point-to-point stream

```python
from __future__ import annotations
from dataclasses import dataclass, field

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

    def run_proc(self) -> ProcessGen:
        for i in range(3):
            words = np.array([i * 10, i * 10 + 1], dtype=np.uint32)
            yield self.process(self.ep.write(words))


@dataclass
class Consumer(SimObj):
    def __post_init__(self) -> None:
        super().__post_init__()
        self.received: list[np.ndarray] = []
        self.ep = StreamIFSlave(
            sim=self.sim,
            bitwidth=32,
            rx_proc=self.on_rx,
            queue_size=16,
        )

    def on_rx(self, words: Words) -> ProcessGen:
        self.received.append(words.copy())
        yield self.env.timeout(0)   # or model processing delay here

    def run_proc(self) -> ProcessGen:
        yield from self.ep.run_proc()


sim = Simulation()
clk = Clock(freq=100e6)

producer = Producer(sim=sim)
consumer = Consumer(sim=sim)

iface = StreamIF(sim=sim, clk=clk, bitwidth=32, latency_init=4.0)
iface.bind("master", producer.ep)
iface.bind("slave",  consumer.ep)

sim.run_sim()
```

The `Consumer.run_proc()` must delegate to `ep.run_proc()` so the slave's receive loop is active during simulation. `Simulation.run_sim()` calls each `SimObj.run_proc()` automatically, so this pattern wires together correctly.

This is a complete, runnable two-[`SimObj`](../sim/simobj.md) simulation — a `Producer` (master) and a `Consumer` (slave) bound over one `StreamIF`, no `HwModule` — and it is the same shape as the toys on the other interface pages. The `yield` / `run_proc` / `ProcessGen` mechanics it relies on are explained in [Process generators](../sim/procgen.md). A `CrossBarIF` variant is the same idea with port-indexed endpoints (`in_0` / `out_0`) — see below.

---

## Crossbar: CrossBarIF

`CrossBarIF` routes bursts from `nports_in` input ports to `nports_out` output ports via a configurable routing function.

### Classes

| Class | Role | Key parameters |
|---|---|---|
| `CrossBarIF` | Interface | `clk`, `bitwidth`, `latency_init`, `nports_in`, `nports_out`, `route_fn` |
| `CrossBarIFInput` | Input (master) endpoint | `bitwidth` |
| `CrossBarIFOutput` | Output (slave) endpoint | `bitwidth`, `rx_proc`, `queue_size` |

Endpoint names follow the pattern `in_0`, `in_1`, …, `out_0`, `out_1`, …

### Routing function

The `route_fn(words, port_in) -> port_out` callable maps each burst to an output port. If not provided, the default is `port_out = port_in % nports_out`.

```python
def route_by_first_word(words: Words, port_in: int) -> int:
    return int(words[0]) % nports_out
```

### Example: 2×2 crossbar

```python
from waveflow.hw.interface import CrossBarIF, CrossBarIFInput, CrossBarIFOutput

xbar = CrossBarIF(
    sim=sim,
    clk=clk,
    nports_in=2,
    nports_out=2,
    bitwidth=32,
    latency_init=2.0,
    route_fn=route_by_first_word,
)

xbar.bind("in_0",  src0.input_ep)
xbar.bind("in_1",  src1.input_ep)
xbar.bind("out_0", sink0.output_ep)
xbar.bind("out_1", sink1.output_ep)
```

The crossbar's `write(words, port_in)` is called internally by `CrossBarIFInput.write(words)` — callers only need the input endpoint's `write` method.

Each `CrossBarIFOutput` endpoint has the same `run_proc()` loop as `StreamIFSlave` and must be started before transfers are sent.

---

## Common patterns

### Checking data in a slave

```python
class Checker(SimObj):
    def __post_init__(self) -> None:
        super().__post_init__()
        self.bursts: list[np.ndarray] = []
        self.ep = StreamIFSlave(sim=self.sim, bitwidth=32, rx_proc=self.rx_proc)

    def rx_proc(self, words: Words) -> ProcessGen:
        self.bursts.append(words.copy())
        yield self.env.timeout(0)

    def run_proc(self) -> ProcessGen:
        yield from self.ep.run_proc()

    def post_sim(self) -> None:
        assert len(self.bursts) == expected_count
```

### Modelling receiver processing delay

Set a non-zero delay in `rx_proc` to model the time the slave spends consuming each burst:

```python
def rx_proc(self, words: Words) -> ProcessGen:
    processing_cycles = len(words) * 2
    yield self.timeout(processing_cycles / self.clk.freq)
```

### Queue depth

`queue_size` on the slave endpoint bounds how many words can be in-flight. Setting `queue_size=None` (default) gives an unbounded queue. For backpressure modelling, set an explicit depth.

---

## Quick reference

```python
from waveflow.hw.interface import (
    StreamIF, StreamIFMaster, StreamIFSlave,
    StreamGetPipelinedStmt, StreamWritePipelinedStmt,
    CrossBarIF, CrossBarIFInput, CrossBarIFOutput,
    Words,
)
from waveflow.hw.clock import Clock
```

| Operation | Code |
|---|---|
| Create interface | `StreamIF(sim=sim, clk=clk, bitwidth=32, latency_init=4.0)` |
| Create master ep | `StreamIFMaster(sim=sim, bitwidth=32)` |
| Create slave ep | `StreamIFSlave(sim=sim, bitwidth=32, rx_proc=fn)` |
| Bind | `iface.bind("master", ep)` |
| Write (from run_proc) | `yield self.process(ep.write(words))` |
| Start slave loop | `yield from ep.run_proc()` |
