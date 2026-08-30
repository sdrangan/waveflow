---
title: Overview
parent: Interfaces
nav_order: 1
audience: python
api: [Interface, InterfaceEndpoint, StreamIF, StreamIFMaster, StreamIFSlave, MMIFMaster, MMIFSlave, Words, Simulation]
summary: "The transactional interface model — Interface vs master/slave endpoint, the Words type, the cycle-based latency model, and the SimPy write/read/bind lifecycle, plus a runnable two-SimObj StreamIF toy."
---

# Overview

## Core concepts

### Interface

An `Interface` is a named object that connects exactly two or more **endpoints**. It owns the latency model and the routing logic for all data passing over the connection. The interface knows the clock frequency, the data bitwidth, and any protocol-specific parameters (queue depth, protocol type, address ranges, and so on).

```python
from waveflow.hw.interface import StreamIF
from waveflow.hw.clock import Clock

clk = Clock(freq=100e6)   # 100 MHz
iface = StreamIF(sim=sim, clk=clk, bitwidth=32, latency_init=4.0)
```

### InterfaceEndpoint

An `InterfaceEndpoint` is the **handle** that a component holds to participate in an interface. There are always two roles:

- **Master endpoint** — initiates transactions (calls `write`, `read`).
- **Slave endpoint** — receives transactions (provides `rx_proc` callbacks).

Endpoints are created by the component that owns them, then registered with an interface via `bind()`.

```python
from waveflow.hw.interface import StreamIFMaster, StreamIFSlave

# Created inside each component's __post_init__
master_ep = StreamIFMaster(sim=sim, bitwidth=32)
slave_ep  = StreamIFSlave(sim=sim, bitwidth=32, rx_proc=self.on_receive)

# Wired together pre-simulation
iface.bind("master", master_ep)
iface.bind("slave",  slave_ep)
```

### Words

Most interfaces transfer data as **numpy arrays of fixed-width integers**, aliased as `Words`.
A [`BramIF`](primitive/bram.md) is the exception: it is *element-typed*, so its storage and its
views carry the element's own dtype rather than a packed word.

```python
from waveflow.hw.interface import Words   # NDArray[uint32] | NDArray[uint64]
import numpy as np

words = np.array([0xA0, 0xA1, 0xA2], dtype=np.uint32)
```

The convention is:
- `bitwidth <= 32` → `dtype=np.uint32`
- `bitwidth <= 64` → `dtype=np.uint64`
- `bitwidth > 64`  → `(n, k)` array of `uint64` in little-endian word order

### Access methods

What an endpoint's methods transfer, and when they happen relative to other work, is specific to the
**primitive** interfaces — the derived ones have their own vocabularies. See
[what an access method says](primitive/index.md#what-an-access-method-says).

### SimPy integration

Interface transactions are modelled as SimPy generator processes. Calling `write` or `read` on a master endpoint returns a generator; the caller must yield it to advance simulation time:

```python
def run_proc(self) -> ProcessGen:
    words = np.array([1, 2, 3], dtype=np.uint32)

    # Blocks until the transfer completes (latency + burst cycles)
    yield self.process(master_ep.write(words))
```

For reads that return data, the result is carried in `proc.value` (the SimPy process return value):

```python
proc = env.process(master_ep.read(nwords=4, global_addr=0x0000))
yield proc
data = proc.value   # numpy array of shape (4,)
```

## A minimal simulation

Two raw [`SimObj`](../sim/simobj.md)s connecting over a `StreamIF` — a `Producer` holding the master
endpoint and a `Consumer` holding the slave endpoint, bound and run in one `Simulation`. No
`HwModule` involved. (The `yield` / `run_proc` / `ProcessGen` mechanics are explained in
[Process generators](../sim/procgen.md).)

```python
from dataclasses import dataclass

import numpy as np

from waveflow.hw.clock import Clock
from waveflow.hw.interface import StreamIF, StreamIFMaster, StreamIFSlave, Words
from waveflow.simulation.simobj import ProcessGen, SimObj
from waveflow.simulation.simulation import Simulation


@dataclass
class Producer(SimObj):
    """Holds the master endpoint; writes each packet over the stream."""

    master: StreamIFMaster | None = None
    packets: list | None = None

    def run_proc(self) -> ProcessGen[None]:
        for packet in self.packets:
            yield from self.master.write(packet)   # blocks for latency + burst cycles
            print(f"{self.name} sent {packet.tolist()}")


@dataclass
class Consumer(SimObj):
    """Holds the slave endpoint; rx_proc fires for each arriving burst."""

    def __post_init__(self) -> None:
        super().__post_init__()
        self.received: list[np.ndarray] = []
        self.slave = StreamIFSlave(sim=self.sim, bitwidth=32, rx_proc=self.on_receive)

    def on_receive(self, words: Words) -> ProcessGen[None]:
        self.received.append(np.array(words, copy=True))
        print(f"{self.name} received {words.tolist()}")
        yield self.timeout(0)


sim = Simulation()
iface = StreamIF(sim=sim, clk=Clock(freq=100e6), bitwidth=32)

producer = Producer(
    name="producer", sim=sim,
    master=StreamIFMaster(sim=sim, bitwidth=32),
    packets=[np.array([1, 2, 3], dtype=np.uint32), np.array([4, 5], dtype=np.uint32)],
)
consumer = Consumer(name="consumer", sim=sim)

iface.bind("master", producer.master)
iface.bind("slave", consumer.slave)

sim.run_sim()
print("consumer received:", [p.tolist() for p in consumer.received])
```

Each burst the producer `write`s lands in `consumer.received` (`[[1, 2, 3], [4, 5]]`): the master
drives, the slave's `rx_proc` fires per burst, and `run_sim()` returns once the producer's `run_proc`
finishes and the slave's receive loop parks on its empty buffer. See [SimObj](../sim/simobj.md) for the
base object and lifecycle.

## Available interface types

The full list, in tiers, is the map on the [Interfaces](./) landing page — it is the one summary
table for this section, so this page does not repeat it. In short: a **primitive** interface lowers
to a real HLS construct ([primitive interfaces](./primitive/)), a **derived** one is a transaction
pattern over a primitive ([derived interfaces](./derived/)), and a simulation-only one does not
lower at all.

## Lifecycle

Interfaces participate in the standard SimPy three-phase lifecycle managed by `Simulation`:

1. **`pre_sim()`** — validate bindings, assign address ranges, set up state.
2. **`run_proc()`** — slave endpoints start their receive loops here (e.g. `StreamIFSlave.run_proc()`).
3. **`post_sim()`** — collect statistics or assert invariants.

`assign_address_ranges()` (for `AXIMMCrossBarIF`) should be called after binding but before `sim.run_sim()`.

## Next steps

- [Stream Interfaces](./primitive/stream.md) — unidirectional streaming with `StreamIF` and `CrossBarIF`
- [MM Interfaces](./primitive/aximm.md) — memory-mapped read/write with `AXIMMCrossBarIF` and `DirectMMIF`
