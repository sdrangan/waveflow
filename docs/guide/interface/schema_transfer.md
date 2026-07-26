---
title: Schema Transfer Interface
parent: Interfaces
nav_order: 6
audience: python
api: [SchemaTransferIF, SchemaTransferIFMaster, SchemaTransferIFSlave, PhysicalTransport, StreamTransport, SimObj, Simulation]
summary: "The logical interface that carries serializable schema objects (DataList / DataUnion) over a transport — write(obj) / rx_proc, the layered transport model, and single- vs multi-type framing, with a runnable two-SimObj toy."
---

# Schema Transfer Interface

`SchemaTransferIF` is a **logical interface** that carries serializable Python objects between simulation components.  The master side calls `write(obj)` with any object that implements `serialize(word_bw)`; the slave side calls `schema_type().deserialize(words, word_bw)` and delivers the result to an `rx_proc` callback or a `simpy.Store` queue.

The interface is **agnostic to framing**: whether `schema_type` is a plain `DataList` (single known type, no header) or a `DataUnion` (multi-type dispatch via header) is entirely the caller's concern.

```
Application layer:   Component.write(obj)         rx_proc(obj)
                           │                            │
Logical layer:    SchemaTransferIFMaster    SchemaTransferIFSlave
                           │                            │
Transport layer:     PhysicalTransport  (StreamTransport | …)
                           │                            │
Physical layer:    StreamIFMaster               StreamIFSlave
```

---

## A minimal simulation

Two raw [`SimObj`](../sim/simobj.md)s sending a schema object master→slave. Each transfer endpoint
owns an internal `StreamIFMaster` / `StreamIFSlave` (`stream_ep`); you complete the physical link by
binding those two `stream_ep`s over a `StreamIF`. No `HwModule`. (The `yield from` / `run_proc`
mechanics are in [Process generators](../sim/procgen.md).)

```python
from dataclasses import dataclass

from waveflow.hw.clock import Clock
from waveflow.hw.dataschema import DataList, IntField
from waveflow.hw.interface import StreamIF
from waveflow.hw.schema_transfer_interface import (
    SchemaTransferIFMaster, SchemaTransferIFSlave,
)
from waveflow.simulation.simobj import ProcessGen, SimObj
from waveflow.simulation.simulation import Simulation

U8 = IntField.specialize(bitwidth=8, signed=False)
S16 = IntField.specialize(bitwidth=16, signed=True)


class SensorPacket(DataList):
    elements = {"temp": S16, "sensor_id": U8}


@dataclass
class Producer(SimObj):
    """Holds the schema master; serializes and sends each packet."""

    master: SchemaTransferIFMaster | None = None

    def run_proc(self) -> ProcessGen[None]:
        for temp, sid in [(-10, 1), (25, 2)]:
            yield from self.master.write(SensorPacket(temp=temp, sensor_id=sid))


@dataclass
class Consumer(SimObj):
    """Holds the schema slave; rx_proc fires with each deserialized packet."""

    def __post_init__(self) -> None:
        super().__post_init__()
        self.received: list[tuple[int, int]] = []
        self.slave = SchemaTransferIFSlave(
            sim=self.sim, schema_type=SensorPacket, bitwidth=32, rx_proc=self.on_packet,
        )

    def on_packet(self, pkt: SensorPacket) -> ProcessGen[None]:
        self.received.append((int(pkt.temp), int(pkt.sensor_id)))
        yield self.timeout(0)


sim = Simulation()
clk = Clock(freq=1e9)

producer = Producer(name="producer", sim=sim, master=SchemaTransferIFMaster(sim=sim, bitwidth=32))
consumer = Consumer(name="consumer", sim=sim)

# Each transfer endpoint owns an internal stream_ep; bind those to wire the physical link.
stream_if = StreamIF(sim=sim, clk=clk)
stream_if.bind("master", producer.master.stream_ep)
stream_if.bind("slave", consumer.slave.stream_ep)

sim.run_sim()
print("consumer received:", consumer.received)
```

Each `SensorPacket` the producer `write`s is serialized, carried over the stream, deserialized, and
delivered to the consumer's `rx_proc` — `consumer.received` ends up `[(-10, 1), (25, 2)]`. See
[SimObj](../sim/simobj.md) for the base object and lifecycle. The detailed single-type and
multi-type (`DataUnion`) walkthroughs are below.

---

## Classes

| Class | Role |
|---|---|
| `PhysicalTransport` | Abstract base: `write_words(words)` + `set_rx_callback(fn)` (internal) |
| `StreamTransport` | Adapter over `StreamIFMaster` / `StreamIFSlave` (built internally by each endpoint) |
| `SchemaTransferIFMaster` | Serializes objects → forwards word bursts over its stream port |
| `SchemaTransferIFSlave` | Receives word bursts → deserializes → delivers to `rx_proc` / queue |
| `SchemaTransferIF` | Optional logical container; validates endpoint types and bitwidth |

---

## The transport layer (internal)

You do **not** construct or pass a transport. Each `SchemaTransferIF` endpoint creates its own
internal `StreamTransport` in `__post_init__` and exposes the underlying stream endpoint as
`.stream_ep`; you wire the physical link by binding those `.stream_ep`s over a shared `StreamIF`
(exactly as the [minimal simulation](#a-minimal-simulation) above does).

`PhysicalTransport` is the ABC the layer is built on (`write_words(words)` +
`set_rx_callback(fn)`), and `StreamTransport` is its only implementation — an adapter whose
`write_words` delegates to the master `stream_ep.write(words)` and whose `set_rx_callback` sets the
slave `stream_ep.rx_proc`. Both are plumbing; the sections below cover the endpoints you actually
construct.

---

## SchemaTransferIFMaster

| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `bitwidth` | `int` | `32` | Word width for serialization |

The master creates an internal `StreamIFMaster` exposed as `.stream_ep` — bind it over a `StreamIF`.

```python
master = SchemaTransferIFMaster(sim=sim, bitwidth=32)
```

**Usage** — from inside a `run_proc`:

```python
def run_proc(self) -> ProcessGen:
    yield from self.schema_master.write(SomePacket(field=value))
```

`write(obj)` calls `obj.serialize(word_bw=self.bitwidth)` then forwards the resulting word array to the transport.  Any object with a `serialize` method works — `DataList`, `DataArray`, or `DataUnion`.

---

## SchemaTransferIFSlave

| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `schema_type` | `type` | — | Class to call `.deserialize(words, word_bw)` on |
| `bitwidth` | `int` | `32` | Word width for deserialization |
| `rx_proc` | `Callable[[Any], ProcessGen] \| None` | `None` | Callback invoked with each deserialized object |
| `pull_mode` | `bool` | `False` | When `True`, disables the push callback and enables `yield from slave.get()` |

The slave creates an internal `StreamIFSlave` exposed as `.stream_ep` — bind it over a `StreamIF`.

```python
slave = SchemaTransferIFSlave(
    sim=sim,
    schema_type=SensorDU,   # DataUnion or DataList subclass
    bitwidth=32,
    rx_proc=self._on_object,
)
```

### Lifecycle

`SchemaTransferIFSlave.pre_sim()` installs `_on_words_received` as the transport's receive callback.  When using `Simulation.run_sim()`, this happens automatically before the event loop starts.

When calling `env.run()` directly (without `Simulation.run_sim()`), call `schema_slave.pre_sim()` manually before `env.run()`.

### Queue

Every slave exposes a `simpy.Store` at `schema_slave.queue`.  Each received object is put into the queue **before** `rx_proc` is called, so consumers can pull objects instead of registering a callback:

```python
def run_proc(self) -> ProcessGen:
    while True:
        event = self.schema_slave.queue.get()
        yield event
        obj = event.value
        yield from self.process_object(obj)
```

---

## SchemaTransferIF

`SchemaTransferIF` is an optional `Interface` container that enforces type and bitwidth consistency when binding endpoints:

```python
from waveflow.hw.schema_transfer_interface import SchemaTransferIF

iface = SchemaTransferIF(sim=sim)
iface.bind("master", master_ep)
iface.bind("slave",  slave_ep)
```

Binding raises `TypeError` if the wrong endpoint class is used for a side, and `ValueError` if the master and slave have different bitwidths.

`SchemaTransferIF` is optional and only validates the logical pair — it does **not** wire the physical link. You still bind each endpoint's `.stream_ep` over a `StreamIF` to connect them (see the [minimal simulation](#a-minimal-simulation)).

---

## Example: single-type transfer

Every transfer carries one known schema; no header is needed.

```python
from dataclasses import dataclass

from waveflow.hw.clock import Clock
from waveflow.hw.dataschema import DataList, IntField
from waveflow.hw.interface import StreamIF
from waveflow.hw.schema_transfer_interface import (
    SchemaTransferIFMaster, SchemaTransferIFSlave,
)
from waveflow.simulation.simulation import Simulation
from waveflow.simulation.simobj import ProcessGen, SimObj

U8  = IntField.specialize(bitwidth=8,  signed=False)
S16 = IntField.specialize(bitwidth=16, signed=True)

class SensorPacket(DataList):
    elements = {"temp_raw": S16, "sensor_id": U8}

# SensorPacket.nwords_per_inst(32) == 1  →  1 word per transfer


@dataclass
class TxComponent(SimObj):
    def __post_init__(self) -> None:
        super().__post_init__()
        self.schema_ep = SchemaTransferIFMaster(sim=self.sim, bitwidth=32)

    def run_proc(self) -> ProcessGen:
        for temp_raw, sid in [(-10, 1), (25, 2), (75, 3)]:
            yield from self.schema_ep.write(SensorPacket(temp_raw=temp_raw, sensor_id=sid))


@dataclass
class RxComponent(SimObj):
    def __post_init__(self) -> None:
        super().__post_init__()
        self.schema_ep = SchemaTransferIFSlave(
            sim=self.sim, schema_type=SensorPacket, bitwidth=32, rx_proc=self.on_packet,
        )

    def on_packet(self, pkt: SensorPacket) -> ProcessGen:
        print(f"temp_raw={int(pkt.temp_raw)}  sensor_id={int(pkt.sensor_id)}")
        yield self.env.timeout(0)


sim = Simulation()
clk = Clock(freq=1e9)

tx = TxComponent(sim=sim)
rx = RxComponent(sim=sim)

# Each transfer endpoint owns an internal stream_ep; bind those over a StreamIF.
stream_if = StreamIF(sim=sim, clk=clk)
stream_if.bind("master", tx.schema_ep.stream_ep)
stream_if.bind("slave",  rx.schema_ep.stream_ep)

sim.run_sim()
```

---

## Example: multi-type transfer (DataUnion)

Multiple payload types share one interface.  The `DataUnion` header carries `schema_id` so the slave can dispatch; `SchemaTransferIF` itself sees only words.

```python
from waveflow.hw.dataunion import (
    DataUnion, DataUnionHdr, SchemaIDField, SchemaRegistry, register_schema,
)

sensor_reg = SchemaRegistry("Sensor")

@register_schema(schema_id=1, registry=sensor_reg)
class TempPacket(DataList):
    elements = {"temp_raw": S16, "sensor_id": U8}

@register_schema(schema_id=2, registry=sensor_reg)
class AccelPacket(DataList):
    elements = {"ax": S16, "ay": S16, "az": S16}

SensorSchemaID = SchemaIDField.specialize(registry=sensor_reg, bitwidth=16)
SensorHdr      = DataUnionHdr.specialize(schema_id_type=SensorSchemaID)
SensorDU       = DataUnion.specialize(hdr_type=SensorHdr)
# SensorDU.nwords_per_inst(32) == 3  →  1 hdr + 2 payload words


# Transmitter
def run_proc(self) -> ProcessGen:
    for payload in [TempPacket(temp_raw=-42, sensor_id=7),
                    AccelPacket(ax=100, ay=-200, az=980)]:
        du = SensorDU()
        du.payload = payload
        yield from self.schema_ep.write(du)


# Receiver — dispatch table
_handlers = {
    TempPacket:  _on_temp,
    AccelPacket: _on_accel,
}

def on_receive(self, du: SensorDU) -> ProcessGen:
    handler = _handlers.get(type(du.payload))
    if handler is not None:
        yield from handler(self, du.payload)

# Slave configuration — only schema_type changes vs. the single-type example;
# bind rx.schema_ep.stream_ep over the StreamIF exactly as before.
rx.schema_ep = SchemaTransferIFSlave(
    sim=sim,
    schema_type=SensorDU,   # ← DataUnion, not DataList
    bitwidth=32,
    rx_proc=rx.on_receive,
)
```

The slave calls `SensorDU().deserialize(words, 32)` which reads the header, looks up the payload class in the registry, and populates `du.payload` with the correct type.

---

## Dispatch patterns

### rx_proc callback (push)

The most common pattern.  `rx_proc` is called once per received object inside the slave's `_on_words_received` generator.

```python
def on_receive(self, du: SensorDU) -> ProcessGen:
    handler = self._handlers.get(type(du.payload))
    if handler is not None:
        yield from handler(du.payload)
```

`rx_proc` must be a generator function (`yield` at least once, e.g. `yield self.env.timeout(0)`).

### Queue poll (pull)

```python
def run_proc(self) -> ProcessGen:
    while True:
        event = self.schema_slave.queue.get()
        yield event
        du = event.value              # SensorDU in multi-type mode
        yield from self.dispatch(du)
```

Both `rx_proc` and the queue are always active simultaneously — the object is enqueued first, then `rx_proc` is called.

---

## Wire footprint

| `schema_type` | Header | Words per transfer |
|---|---|---|
| `type[DataList]` | None | `DataList.nwords_per_inst(word_bw)` |
| `type[DataUnion]` | `DataUnionHdr` (schema_id) | `DataUnion.nwords_per_inst(word_bw)` |

---

## Quick reference

```python
from waveflow.hw.schema_transfer_interface import (
    PhysicalTransport,
    StreamTransport,
    SchemaTransferIFMaster,
    SchemaTransferIFSlave,
    SchemaTransferIF,
)
```

| Operation | Code |
|---|---|
| Create master | `SchemaTransferIFMaster(sim=sim, bitwidth=32)` |
| Create slave | `SchemaTransferIFSlave(sim=sim, schema_type=T, bitwidth=32, rx_proc=fn)` |
| Wire the physical link | `stream_if.bind("master", master.stream_ep); stream_if.bind("slave", slave.stream_ep)` |
| Transmit (from run_proc) | `yield from master.write(obj)` |
| Register callback (manual) | `slave.pre_sim()` before `env.run()` |
| Poll queue | `event = slave.queue.get(); yield event; obj = event.value` |
| Wire footprint (single type) | `MySchema.nwords_per_inst(32)` |
| Wire footprint (DataUnion) | `MyDU.nwords_per_inst(32)` |

See also: [`schema_transfer_demo.py`](../../../examples/interface/schema_transfer_demo.py) for a complete runnable example.
