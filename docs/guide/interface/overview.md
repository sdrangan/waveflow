---
title: Overview
parent: Interfaces
nav_order: 1
audience: python
api: [Interface, InterfaceEndpoint, StreamIF, MMIFMaster, MMIFSlave, Words]
summary: "The transactional interface model — Interface vs master/slave endpoint, the Words type, the cycle-based latency model, and the SimPy write/read/bind lifecycle."
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

All interfaces transfer data as **numpy arrays of fixed-width integers**, aliased as `Words`:

```python
from waveflow.hw.interface import Words   # NDArray[uint32] | NDArray[uint64]
import numpy as np

words = np.array([0xA0, 0xA1, 0xA2], dtype=np.uint32)
```

The convention is:
- `bitwidth <= 32` → `dtype=np.uint32`
- `bitwidth <= 64` → `dtype=np.uint64`
- `bitwidth > 64`  → `(n, k)` array of `uint64` in little-endian word order

### Latency model

All interfaces model transfer time as a **cycle count divided by clock frequency**. For a transfer of `nwords` over a channel with `latency_init` setup cycles and clock frequency `clk.freq`:

```
transfer_time = (latency_init + nwords) / clk.freq   [seconds]
```

The `latency_init` captures wire delay, arbitration overhead, and other fixed-cost cycles. Each additional word contributes one cycle (one beat on the bus).

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

## Available interface types

| Interface | Module | Use case |
|---|---|---|
| `StreamIF` | `waveflow.hw.interface` | Unidirectional data stream (AXI4-Stream or HLS stream) |
| `CrossBarIF` | `waveflow.hw.interface` | Port-indexed stream crossbar (n inputs × m outputs) |
| `AXIMMCrossBarIF` | `waveflow.hw.aximm` | AXI memory-mapped crossbar; endpoints are `MMIFMaster` / `MMIFSlave` |
| `DirectMMIF` | `waveflow.hw.aximm` | Point-to-point MM link (BRAM / local scratchpad) |

## Lifecycle

Interfaces participate in the standard SimPy three-phase lifecycle managed by `Simulation`:

1. **`pre_sim()`** — validate bindings, assign address ranges, set up state.
2. **`run_proc()`** — slave endpoints start their receive loops here (e.g. `StreamIFSlave.run_proc()`).
3. **`post_sim()`** — collect statistics or assert invariants.

`assign_address_ranges()` (for `AXIMMCrossBarIF`) should be called after binding but before `sim.run_sim()`.

## Next steps

- [Stream Interfaces](./stream.md) — unidirectional streaming with `StreamIF` and `CrossBarIF`
- [MM Interfaces](./aximm.md) — memory-mapped read/write with `AXIMMCrossBarIF` and `DirectMMIF`
