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

**Fixed length, by design.** Every `DataUnion` message is the same number of words: the header, then
the payload **from the next word boundary**, in its own type's word layout, zero-padded to the longest. That is what lets a union be read like any other schema -- a reader takes
`nwords_per_inst` words and decodes them, never deciding a length from the data -- at the cost of the
padding on short messages. A design that cannot afford the padding sends one `DataList` per message
type instead, each beginning with the same tag; a reader then reads the tag first and the rest by that
type's schema.

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

Use the `@register_schema` decorator to associate an ID with a `DataList` class.  IDs are non-negative integers -- 0 is allowed; omitting `schema_id` auto-assigns the next available value (`max + 1`, or 0 in an empty registry).

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

> **On a stream.** A `DataUnion` is a `DataSchema` like a `DataList`: a stream endpoint's `write` takes
> it (one message, one burst), `get_schema(SensorDU)` reads one, it can be a field of a `DataList`, and
> its generated C++ has every reader and writer -- array, stream, AXI4-Stream, and framed with
> `DataSchemaStep(..., framed=True)`. What it cannot be is variable-length: for a variable number of
> results on a kernel's stream -- the
> [variable-length output](../../patterns/command_response.md#variable-length-output-a-tagged-union-ending-in-end)
> of the command-response pattern -- send one `DataList` per message type, each beginning with the same
> `EnumField` tag ([Tagged unions on a stream](tagged_union.md)).

---

## Receiving a DataUnion

On the receive side, call `deserialize` on a fresh instance.  It reads the header, looks up the payload class in the registry, and populates `du.payload` with the correct type:

```python
du_rx = SensorDU().deserialize(words, word_bw=32)

print(type(du_rx.payload))   # → AccelPacket
print(du_rx.schema_id)       # → 3
print(du_rx.payload.ax)      # → 100
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

A `DataSchemaStep` (or `SensorDU.gen_include(cfg, word_bw_supported=[32, 64])` on its own) generates
a C++ struct with the header, **one member per registered type**, typed accessors, and the readers and
writers every schema has:

```cpp
struct SensorDataUnion {
    SensorHdr header;
    TempPacket temp_packet;      // one member per registered type, snake case
    PressPacket press_packet;
    AccelPacket accel_packet;

    TempPacket get_TempPacket() const;              // the member
    void set_TempPacket(const TempPacket& p);       // the member, and header.schema_id
    ...

    template<int word_bw> static constexpr int nwords();
    template<int word_bw> void write_array(ap_uint<word_bw> x[]) const;
    template<int word_bw> void read_array(const ap_uint<word_bw> x[]);
    template<int word_bw> void write_stream(hls::stream<ap_uint<word_bw>>& s) const;
    template<int word_bw> void read_stream(hls::stream<ap_uint<word_bw>>& s);
    template<int word_bw> void write_axi4_stream(hls::stream<streamutils::axi4s_word<word_bw>>& s,
                                                 bool tlast = true) const;
    template<int word_bw> void read_axi4_stream(hls::stream<streamutils::axi4s_word<word_bw>>& s);
};
```

A writer sends the header, then a `switch` on `schema_id` writes that type's payload, padded to the
longest; a reader reads the header and the padded payload words, and the `switch` decodes them into the
member `schema_id` names.

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
