---
title: Data Unions
parent: Python
grand_parent: Data Schemas
nav_order: 4
audience: python
api: [DataUnion]
summary: "DataUnion — a tagged union: a schema-id header selecting one of several registered payload schemas, with a fixed wire footprint."
---

# Data Unions

A `DataUnion` is a **fixed-size envelope** that carries one of several registered `DataList` payloads over a single interface.  The envelope includes a header that encodes which payload type is present, so the receiver can deserialize without out-of-band signalling.

```
┌──────────────────────┬──────────────────────────────────┐
│  DataUnionHdr        │  payload (padded to max width)    │
│  schema_id  [16 b]   │  TempPacket | AccelPacket | …     │
└──────────────────────┴──────────────────────────────────┘
```

The total word count is fixed regardless of which payload is present:

```
nwords = hdr.nwords_per_inst(word_bw) + max(payload.nwords_per_inst(word_bw)
                                            for payload in registry)
```

---

## Building a DataUnion step by step

### 1. Create a registry

`SchemaRegistry` is a named container that maps integer IDs to `DataList` classes.  There is no global singleton; each design creates its own.

```python
from waveflow.hw.dataunion import SchemaRegistry

sensor_reg = SchemaRegistry("Sensor")
```

The name (`"Sensor"`) is used as a prefix for generated C++ types.

---

### 2. Register payload schemas

Use the `@register_schema` decorator to associate an ID with a `DataList` class.  IDs must be positive integers; omitting `schema_id` auto-assigns the next available value.

```python
from waveflow.hw.dataschema import DataList, IntField
from waveflow.hw.dataunion import register_schema

U8  = IntField.specialize(bitwidth=8,  signed=False)
S16 = IntField.specialize(bitwidth=16, signed=True)
U16 = IntField.specialize(bitwidth=16, signed=False)

@register_schema(schema_id=1, registry=sensor_reg)
class TempPacket(DataList):
    elements = {"temp_raw": S16, "sensor_id": U8}

@register_schema(schema_id=2, registry=sensor_reg)
class PressPacket(DataList):
    elements = {"pressure_pa": U16, "sensor_id": U8}

@register_schema(registry=sensor_reg)     # auto-assigned: 3
class AccelPacket(DataList):
    elements = {"ax": S16, "ay": S16, "az": S16}
```

Each registered class can be retrieved by ID or looked up in the other direction:

```python
sensor_reg.get_class(1)            # → TempPacket
sensor_reg.get_id(AccelPacket)     # → 3
sensor_reg.next_id()               # → 4  (next auto value)

for sid, cls in sensor_reg.items():
    print(sid, cls.__name__)
```

---

### 3. Build a header

`SchemaIDField` is a validated integer field whose legal values are exactly the IDs in a registry.  Assigning an unregistered ID raises `ValueError` immediately.

```python
from waveflow.hw.dataunion import SchemaIDField, DataUnionHdr

SensorSchemaID = SchemaIDField.specialize(registry=sensor_reg, bitwidth=16)
SensorHdr      = DataUnionHdr.specialize(schema_id_type=SensorSchemaID)
```

`DataUnionHdr.specialize` produces a `DataList` subclass whose `elements` dict contains only the `schema_id` field (and optionally a `length` field — see below).

```python
SensorHdr.get_bitwidth()            # → 16
SensorHdr.nwords_per_inst(32)       # → 1
```

#### Optional: LengthField

For protocols that carry variable-length payloads you can add a word-count field to the header:

```python
from waveflow.hw.dataunion import LengthField

Length16  = LengthField.specialize(bitwidth=16)
SensorHdr = DataUnionHdr.specialize(
    schema_id_type=SensorSchemaID,
    length_type=Length16,
)
```

`DataUnion` itself always serializes to a fixed `nwords_per_inst` words, so `LengthField` is only useful when the raw header is decoded independently of `DataUnion`.

---

### 4. Specialize DataUnion

`DataUnion.specialize(hdr_type)` produces a cached subclass tied to a specific header (and therefore a specific registry).

```python
from waveflow.hw.dataunion import DataUnion

SensorDU = DataUnion.specialize(hdr_type=SensorHdr)
```

Key class-level attributes derived automatically:

| Attribute | Meaning |
|---|---|
| `SensorDU.hdr_type` | `SensorHdr` |
| `SensorDU.registry` | `sensor_reg` |
| `SensorDU.max_payload_bw()` | bit width of the largest registered payload |
| `SensorDU.nwords_per_inst(32)` | total words per transfer at 32-bit word width |

---

## Transmitting a DataUnion

Set the `payload` attribute — this automatically updates the `schema_id` in the header:

```python
du = SensorDU()
du.payload = AccelPacket(ax=100, ay=-200, az=980)

print(du.schema_id)    # → 3  (auto-set from registry)

words = du.serialize(word_bw=32)
# words is a 1D numpy array of uint32 with nwords_per_inst(32) elements
```

---

## Receiving a DataUnion

On the receive side, call `deserialize` on a fresh instance.  It reads the header, looks up the payload class in the registry, and populates `du.payload` with the correct type:

```python
du_rx = SensorDU().deserialize(words, word_bw=32)

print(type(du_rx.payload))   # → AccelPacket
print(du_rx.schema_id)       # → 3
print(int(du_rx.payload.ax)) # → 100
```

Deserializing a word array whose `schema_id` is not in the registry raises `ValueError`.

---

## Dispatch patterns

### Dispatch table (recommended)

```python
_handlers = {
    TempPacket:  _on_temp,
    AccelPacket: _on_accel,
    PressPacket: _on_press,
}

def on_receive(du: SensorDU) -> ProcessGen:
    handler = _handlers.get(type(du.payload))
    if handler is not None:
        yield from handler(du.payload)
```

### Queue poll

```python
async def run_proc(self):
    while True:
        event = self.schema_slave.queue.get()
        yield event
        du = event.value
        yield from dispatch(du.payload)
```

---

## Wire format

| `schema_type` passed to slave | Header | Payload |
|---|---|---|
| `type[DataList]` subclass | None — payload only | Fixed size per schema |
| `type[DataUnion]` subclass | `DataUnionHdr` (schema_id) | Padded to `max_payload_bw` |

A typed stream transfer is agnostic to which case applies — `write(obj)` calls `obj.serialize(word_bw)` and `get_schema(T)` calls `T().deserialize(words, word_bw)`.  Both `DataList` and `DataUnion` implement the same serialize/deserialize surface.

---

## C++ code generation

`DataUnion` can generate a Vitis HLS-compatible C++ struct with templated `write_array` / `read_array` methods and typed payload accessors:

```python
path = SensorDU.gen_include(word_bw_supported=[32, 64])
```

The generated struct looks like:

```cpp
struct SensorDataUnion {
    SensorHdr header;
    ap_uint<48> payload_bits;   // max_payload_bw across all schemas

    static constexpr int max_payload_bw = 48;

    template<int WORD_BW>
    void write_array(ap_uint<WORD_BW> x[]) const;   // serialize

    template<int WORD_BW>
    void read_array(const ap_uint<WORD_BW> x[]);    // deserialize

    // Typed accessors
    TempPacket  get_TempPacket()  const;
    void        set_TempPacket(const TempPacket&);
    AccelPacket get_AccelPacket() const;
    void        set_AccelPacket(const AccelPacket&);
    PressPacket get_PressPacket() const;
    void        set_PressPacket(const PressPacket&);
};
```

---

## Quick reference

```python
from waveflow.hw.dataunion import (
    SchemaRegistry,
    register_schema,
    SchemaIDField,
    LengthField,
    DataUnionHdr,
    DataUnion,
)
```

| Operation | Code |
|---|---|
| Create registry | `reg = SchemaRegistry("Name")` |
| Register schema (explicit ID) | `@register_schema(schema_id=1, registry=reg)` |
| Register schema (auto ID) | `@register_schema(registry=reg)` |
| Look up class by ID | `reg.get_class(sid)` |
| Look up ID by class | `reg.get_id(MySchema)` |
| Build schema ID field | `SID = SchemaIDField.specialize(registry=reg, bitwidth=16)` |
| Build header type | `Hdr = DataUnionHdr.specialize(schema_id_type=SID)` |
| Build union type | `DU = DataUnion.specialize(hdr_type=Hdr)` |
| Transmit | `du = DU(); du.payload = MyPayload(...); du.serialize(32)` |
| Receive | `du = DU().deserialize(words, 32); du.payload` |
| Wire footprint | `DU.nwords_per_inst(32)` |
| Generate C++ | `DU.gen_include(word_bw_supported=[32])` |
