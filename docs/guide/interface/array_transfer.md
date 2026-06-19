---
title: Array Transfer Interface
parent: Interfaces
nav_order: 6
audience: python
api: [ArrayTransferIF, ArrayTransferIFMaster, ArrayTransferIFSlave, StreamTransport, SimObj, Simulation]
summary: "The logical interface that carries a variable-length typed array over a transport — write(elements) with a numpy fast path, push (rx_proc) vs pull (get(count)) receive, and TLAST length validation, with a runnable two-SimObj toy."
---

# Array Transfer Interface

`ArrayTransferIF` is a **logical interface** that carries a variable-length array of typed elements between simulation components.  The element type can be any `DataSchema` subclass — a scalar field (`Float32`, `U8`), a composite `DataList`, or a `DataArray`.

`ArrayTransferIF` generalises `SchemaTransferIF`: a single-element array transfer with `count=1` is equivalent to a schema transfer, and the implementations share the same `PhysicalTransport` layer.

```
Application layer:   Component.write(elements)    rx_proc(elements) / get(count)
                           │                                  │
Logical layer:   ArrayTransferIFMaster              ArrayTransferIFSlave
                           │                                  │
Transport layer:     PhysicalTransport       (StreamTransport | …)
                           │                                  │
Physical layer:    StreamIFMaster                    StreamIFSlave
```

---

## Classes

| Class | Role |
|---|---|
| `ArrayTransferIFMaster` | Serializes an element list → one word burst → transport |
| `ArrayTransferIFSlave` | Receives a word burst → deserializes → delivers via `rx_proc` or `get(count)` |
| `ArrayTransferIF` | Optional logical container; validates endpoint types, `element_type`, and `bitwidth` |

`PhysicalTransport` and `StreamTransport` are shared with `SchemaTransferIF`; see the [Schema Transfer Interface](schema_transfer.md) page.

---

## A minimal simulation

Two raw [`SimObj`](../sim/simobj.md)s sending one variable-length array master→slave. Each transfer
endpoint owns an internal `stream_ep`; bind those over a `StreamIF` to wire the physical link. No
`HwComponent`. (The `yield from` / `run_proc` mechanics are in
[Process generators](../sim/procgen.md).)

```python
from dataclasses import dataclass

import numpy as np

from waveflow.hw.clock import Clock
from waveflow.hw.dataschema import FloatField
from waveflow.hw.interface import StreamIF
from waveflow.hw.schema_transfer_interface import (
    ArrayTransferIFMaster, ArrayTransferIFSlave,
)
from waveflow.simulation.simobj import ProcessGen, SimObj
from waveflow.simulation.simulation import Simulation

Float32 = FloatField.specialize(bitwidth=32)


@dataclass
class Producer(SimObj):
    """Holds the array master; sends one variable-length burst of samples."""

    master: ArrayTransferIFMaster | None = None

    def run_proc(self) -> ProcessGen[None]:
        samples = np.array([1.0, -2.5, 3.14], dtype=np.float32)
        yield from self.master.write(samples)     # one packet, TLAST on the last element


@dataclass
class Consumer(SimObj):
    """Holds the array slave; rx_proc fires once with the whole received array."""

    def __post_init__(self) -> None:
        super().__post_init__()
        self.received: np.ndarray | None = None
        self.slave = ArrayTransferIFSlave(
            sim=self.sim, element_type=Float32, bitwidth=32, rx_proc=self.on_samples,
        )

    def on_samples(self, elements: np.ndarray) -> ProcessGen[None]:
        self.received = elements                  # np.ndarray[float32]
        yield self.timeout(0)


sim = Simulation()
clk = Clock(freq=1e9)

producer = Producer(
    name="producer", sim=sim,
    master=ArrayTransferIFMaster(sim=sim, element_type=Float32, bitwidth=32),
)
consumer = Consumer(name="consumer", sim=sim)

# Each transfer endpoint owns an internal stream_ep; bind those to wire the physical link.
stream_if = StreamIF(sim=sim, clk=clk)
stream_if.bind("master", producer.master.stream_ep)
stream_if.bind("slave", consumer.slave.stream_ep)

sim.run_sim()
print("consumer received:", consumer.received.tolist())
```

The producer `write`s the three-element array as one burst; the slave infers the element count from
the burst length and delivers the whole `np.ndarray[float32]` to `rx_proc`, so `consumer.received`
is `[1.0, -2.5, 3.14]`. See [SimObj](../sim/simobj.md) for the base object and lifecycle; push- vs
pull-mode receive is detailed below.

---

## ArrayTransferIFMaster

| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `transport` | `PhysicalTransport` | — | Physical layer to transmit through |
| `element_type` | `type[DataSchema]` | — | Schema class for each element |
| `bitwidth` | `int` | `32` | Word width for serialization |

```python
master = ArrayTransferIFMaster(
    sim=sim,
    transport=transport,
    element_type=Float32,
    bitwidth=32,
)
```

**Usage** — from inside a `run_proc`:

```python
def run_proc(self) -> ProcessGen[None]:
    samples = np.array([1.0, -2.5, 3.14], dtype=np.float32)
    yield from self.arr_master.write(samples)
```

`write(elements)` serializes each element with `element_type.nwords_per_inst(bitwidth)` words, concatenates them into one burst, and forwards the burst to the transport as a single AXI-Stream packet (TLAST asserted on the last word).

**Numpy fast path** — when `element_type` is a scalar `FloatField` or `IntField` and `elements` is a `np.ndarray`, the array is converted to words in a single vectorized operation (no per-element schema instance allocation):

```python
yield from self.arr_master.write(np.array([1.0, -2.5, 3.14], dtype=np.float32))  # Float32
yield from self.arr_master.write(np.array([10, 20, 30], dtype=np.uint8))          # U8
```

`elements` may also contain schema instances or raw Python values — anything that `element_type(value)` accepts (slow path):

```python
yield from self.arr_master.write([Float32(1.0), Float32(-2.5)])  # schema instances
yield from self.arr_master.write([1.0, -2.5, 3.14])              # raw values → Float32
```

---

## ArrayTransferIFSlave

| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `transport` | `PhysicalTransport` | — | Physical layer to receive from |
| `element_type` | `type[DataSchema]` | — | Schema class for each element |
| `bitwidth` | `int` | `32` | Word width for deserialization |
| `rx_proc` | `Callable[[list], ProcessGen[None]] \| None` | `None` | Push-mode callback |
| `pull_mode` | `bool` | `False` | When `True`, enables `get(count)` and disables the push callback |

Two receive modes are available.

### Push mode (default)

Set `rx_proc` to a callback and call `pre_sim()`.  When a burst arrives, the element count is inferred from the burst length divided by `element_type.nwords_per_inst(bitwidth)`, and `rx_proc(elements)` is called with the result.

For scalar `FloatField` / `IntField` element types, `elements` is a `np.ndarray` of the element's native dtype (e.g. `np.float32`).  For composite element types it is a `list` of deserialized instances.

```python
def on_samples(self, elements: np.ndarray) -> ProcessGen[None]:
    # elements is np.ndarray[float32] — no per-element boxing
    process(elements)
    yield self.env.timeout(0)

slave = ArrayTransferIFSlave(
    sim=sim,
    transport=transport,
    element_type=Float32,
    bitwidth=32,
    rx_proc=self.on_samples,
)
slave.pre_sim()   # installs the receive callback
```

### Pull mode

Set `pull_mode=True` (and do not set `rx_proc`).  The owning component drives sequencing by calling `get(count=n)` directly.  An **exact** element count is required; the burst length is validated against `count * element_type.nwords_per_inst(bitwidth)`.

```python
slave = ArrayTransferIFSlave(
    sim=sim,
    transport=transport,
    element_type=Float32,
    bitwidth=32,
    pull_mode=True,
)
```

```python
# Inside the component's run_proc:
samples = yield from self.samp_slave.get(count=nsamp)
# samples is np.ndarray[float32] — use directly in numpy operations
values = samples.astype(float)
```

**TLAST validation** — `get(count)` raises `RuntimeError` if the burst does not match the expected length:

| Condition | Message |
|---|---|
| `actual_words < count * nwords_per_elem` | `TLAST early: expected N words …` |
| `actual_words > count * nwords_per_elem` | `Missing TLAST: expected N words …` |

This matches the error contract in the generated C++ utilities (`TLAST_EARLY_SAMP_IN`, `NO_TLAST_SAMP_IN`).

### Lifecycle

In push mode, `pre_sim()` installs `_on_words_received` as the transport's receive callback.  When using `Simulation.run_sim()` this is called automatically.  When driving `env.run()` directly, call `slave.pre_sim()` manually.

In pull mode, `pre_sim()` is a no-op and the physical slave's `run_proc` exits immediately, leaving the data buffer available for `get()`.

---

## ArrayTransferIF

`ArrayTransferIF` is an optional `Interface` container that enforces consistency when binding endpoints:

```python
from waveflow.hw.schema_transfer_interface import ArrayTransferIF

iface = ArrayTransferIF(sim=sim)
iface.bind("master", master_ep)
iface.bind("slave",  slave_ep)
```

Binding raises:
- `TypeError` if the wrong endpoint class is used for a side
- `ValueError` if master and slave have different `element_type` or `bitwidth`

---

## Example: push mode

The master sends a burst; the slave infers element count from burst length.

```python
import numpy as np
from waveflow.hw.clock import Clock
from waveflow.hw.dataschema import FloatField
from waveflow.hw.interface import StreamIF, StreamIFMaster, StreamIFSlave
from waveflow.hw.schema_transfer_interface import (
    ArrayTransferIFMaster, ArrayTransferIFSlave, StreamTransport,
)
from waveflow.simulation.simulation import Simulation
from waveflow.simulation.simobj import ProcessGen, SimObj

Float32 = FloatField.specialize(bitwidth=32)

sim = Simulation()
clk = Clock(freq=1e9)

stream_if     = StreamIF(sim=sim, clk=clk)
stream_master = StreamIFMaster(sim=sim, bitwidth=32)
stream_slave  = StreamIFSlave(sim=sim, bitwidth=32)
stream_if.bind("master", stream_master)
stream_if.bind("slave",  stream_slave)

transport = StreamTransport(master_ep=stream_master, slave_ep=stream_slave)

received: list[np.ndarray] = []

def on_samples(elements: np.ndarray) -> ProcessGen[None]:
    received.append(elements)   # np.ndarray[float32]
    yield sim.env.timeout(0)

arr_master = ArrayTransferIFMaster(sim=sim, transport=transport, element_type=Float32, bitwidth=32)
arr_slave  = ArrayTransferIFSlave(
    sim=sim, transport=transport, element_type=Float32,
    bitwidth=32, rx_proc=on_samples,
)
arr_slave.pre_sim()

def tx():
    yield from arr_master.write(np.array([1.0, 2.0, 3.0], dtype=np.float32))

sim.env.process(stream_slave.run_proc())
sim.env.process(tx())
sim.env.run(until=sim.env.timeout(10))
# received[0] == np.array([1.0, 2.0, 3.0], dtype=np.float32)
```

---

## Example: pull mode (PolyAccel pattern)

The motivating use case: a component receives a typed header first, then uses the header's `nsamp` field to pull the right number of samples from the same physical stream.

```python
class PolyAccelSim(SimObj):
    def __post_init__(self) -> None:
        super().__post_init__()
        self.stream_slave = StreamIFSlave(sim=self.sim, bitwidth=32)

        transport = StreamTransport(master_ep=..., slave_ep=self.stream_slave)

        self.cmd_slave = SchemaTransferIFSlave(
            sim=self.sim, transport=transport,
            schema_type=PolyCmdHdr, bitwidth=32,
            pull_mode=True,
        )
        self.samp_slave = ArrayTransferIFSlave(
            sim=self.sim, transport=transport,
            element_type=Float32, bitwidth=32,
            pull_mode=True,
        )

    def run_proc(self) -> ProcessGen[None]:
        while True:
            cmd     = yield from self.cmd_slave.get()                        # PolyCmdHdr
            samples = yield from self.samp_slave.get(count=cmd.nsamp)        # np.ndarray[float32]
            _, out, _ = self.accel.evaluate(cmd, samples.astype(float))
            yield from self.resp_master.write(...)
```

Both `cmd_slave` and `samp_slave` share the same `transport` (and therefore the same physical `StreamIFSlave`).  Because `run_proc` is a sequential coroutine, there is no contention — `cmd_slave.get()` consumes the first burst, then `samp_slave.get(count=nsamp)` consumes the second.

---

## Synthesis mapping

The **synthesizable side** — the generated `<element>_array_utils` calls a kernel uses to move this
transfer over an `m_axi` / stream port (`write_axi4_stream_lane` / `read_axi4_stream_lane`, or
`read_array_slice` for a resident array) — is documented, with the Python→C++ mapping table, in
[Custom Hooks: kernel patterns](../custom_hooks/patterns.md#mapping-the-python-transfer-interfaces-to-the-kernel).

---

## Wire footprint

| `element_type` | Words per element | Words per `count`-element transfer |
|---|---|---|
| `Float32` (32-bit, word_bw=32) | 1 | `count` |
| `U8` (8-bit, word_bw=32) | 1 | `count` |
| `S16` (16-bit, word_bw=32) | 1 | `count` |
| `SomeDataList` | `SomeDataList.nwords_per_inst(word_bw)` | `count × nwords_per_inst` |

Elements are packed tightly; no padding is inserted between elements.

---

## Quick reference

```python
from waveflow.hw.schema_transfer_interface import (
    ArrayTransferIFMaster,
    ArrayTransferIFSlave,
    ArrayTransferIF,
    StreamTransport,
)
```

| Operation | Code |
|---|---|
| Create transport | `StreamTransport(master_ep=m, slave_ep=s)` |
| Create master | `ArrayTransferIFMaster(sim=sim, transport=t, element_type=T, bitwidth=32)` |
| Create slave (push) | `ArrayTransferIFSlave(sim=sim, transport=t, element_type=T, bitwidth=32, rx_proc=fn)` |
| Create slave (pull) | `ArrayTransferIFSlave(sim=sim, transport=t, element_type=T, bitwidth=32, pull_mode=True)` |
| Transmit (numpy fast path) | `yield from master.write(np.array(..., dtype=np.float32))` |
| Transmit (schema / raw values) | `yield from master.write([v1, v2, …])` |
| Receive (push, manual setup) | `slave.pre_sim()` before `env.run()` |
| Receive (pull) | `arr = yield from slave.get(count=n)` — `np.ndarray` for scalar types |
| Words per element | `element_type.nwords_per_inst(bitwidth)` |

See also: [Schema Transfer Interface](schema_transfer.md) for the shared `PhysicalTransport` abstraction.
