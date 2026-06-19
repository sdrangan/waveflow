---
title: Schema Transfer Interface
parent: Interfaces
nav_order: 5
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
binding those two `stream_ep`s over a `StreamIF`. No `HwComponent`. (The `yield from` / `run_proc`
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
| `PhysicalTransport` | Abstract base: `write_words(words)` + `set_rx_callback(fn)` |
| `StreamTransport` | Adapter over `StreamIFMaster` / `StreamIFSlave` |
| `SchemaTransferIFMaster` | Serializes objects → forwards word bursts to transport |
| `SchemaTransferIFSlave` | Receives word bursts → deserializes → delivers to `rx_proc` / queue |
| `SchemaTransferIF` | Optional logical container; validates endpoint types and bitwidth |

---

## PhysicalTransport

`PhysicalTransport` is an ABC with two methods:

```python
class PhysicalTransport(ABC):

    @abstractmethod
    def write_words(self, words: Words) -> ProcessGen:
        """Transmit a word burst through the physical endpoint."""

    @abstractmethod
    def set_rx_callback(self, callback: Callable[[Words], ProcessGen]) -> None:
        """Register the callback invoked when a word burst arrives."""
```

The only concrete implementation shipped today is `StreamTransport`.

---

## StreamTransport

`StreamTransport` wraps a `StreamIFMaster` / `StreamIFSlave` pair:

```python
from waveflow.hw.schema_transfer_interface import StreamTransport

transport = StreamTransport(
    master_ep=stream_master,   # StreamIFMaster
    slave_ep=stream_slave,     # StreamIFSlave
)
```

`write_words` delegates to `stream_master.write(words)`.  `set_rx_callback` sets `stream_slave.rx_proc = callback`.

---

## SchemaTransferIFMaster

| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `transport` | `PhysicalTransport` | — | Physical layer to transmit through |
| `bitwidth` | `int` | `32` | Word width for serialization |

```python
master = SchemaTransferIFMaster(sim=sim, transport=transport, bitwidth=32)
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
| `transport` | `PhysicalTransport` | — | Physical layer to receive from |
| `schema_type` | `type` | — | Class to call `.deserialize(words, word_bw)` on |
| `bitwidth` | `int` | `32` | Word width for deserialization |
| `rx_proc` | `Callable[[Any], ProcessGen] \| None` | `None` | Callback invoked with each deserialized object |

```python
slave = SchemaTransferIFSlave(
    sim=sim,
    transport=transport,
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

`SchemaTransferIF` is not required for the transport to function — the transport operates through the master/slave endpoint pair directly.

---

## Example: single-type transfer

Every transfer carries one known schema; no header is needed.

```python
from waveflow.hw.clock import Clock
from waveflow.hw.dataschema import DataList, IntField
from waveflow.hw.interface import StreamIF, StreamIFMaster, StreamIFSlave
from waveflow.hw.schema_transfer_interface import (
    SchemaTransferIFMaster, SchemaTransferIFSlave, StreamTransport,
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
        self.stream_ep = StreamIFMaster(sim=self.sim, bitwidth=32)
        self.schema_ep: SchemaTransferIFMaster | None = None

    def run_proc(self) -> ProcessGen:
        for temp_raw, sid in [(-10, 1), (25, 2), (75, 3)]:
            yield from self.schema_ep.write(SensorPacket(temp_raw=temp_raw, sensor_id=sid))


@dataclass
class RxComponent(SimObj):
    def __post_init__(self) -> None:
        super().__post_init__()
        self.stream_ep = StreamIFSlave(sim=self.sim, bitwidth=32)
        self.schema_ep: SchemaTransferIFSlave | None = None

    def on_packet(self, pkt: SensorPacket) -> ProcessGen:
        print(f"temp_raw={int(pkt.temp_raw)}  sensor_id={int(pkt.sensor_id)}")
        yield self.env.timeout(0)


sim = Simulation()
clk = Clock(freq=1e9)

tx = TxComponent(sim=sim)
rx = RxComponent(sim=sim)

# Physical layer
stream_if = StreamIF(sim=sim, clk=clk)
stream_if.bind("master", tx.stream_ep)
stream_if.bind("slave",  rx.stream_ep)

# Logical layer
transport = StreamTransport(master_ep=tx.stream_ep, slave_ep=rx.stream_ep)
tx.schema_ep = SchemaTransferIFMaster(sim=sim, transport=transport, bitwidth=32)
rx.schema_ep = SchemaTransferIFSlave(
    sim=sim, transport=transport,
    schema_type=SensorPacket, bitwidth=32,
    rx_proc=rx.on_packet,
)

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

# Slave configuration
rx.schema_ep = SchemaTransferIFSlave(
    sim=sim, transport=transport,
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
| Create transport | `StreamTransport(master_ep=m, slave_ep=s)` |
| Create master | `SchemaTransferIFMaster(sim=sim, transport=t, bitwidth=32)` |
| Create slave | `SchemaTransferIFSlave(sim=sim, transport=t, schema_type=T, bitwidth=32, rx_proc=fn)` |
| Transmit (from run_proc) | `yield from master.write(obj)` |
| Register callback (manual) | `slave.pre_sim()` before `env.run()` |
| Poll queue | `event = slave.queue.get(); yield event; obj = event.value` |
| Wire footprint (single type) | `MySchema.nwords_per_inst(32)` |
| Wire footprint (DataUnion) | `MyDU.nwords_per_inst(32)` |

See also: [`schema_transfer_demo.py`](../../../examples/interface/schema_transfer_demo.py) for a complete runnable example.
