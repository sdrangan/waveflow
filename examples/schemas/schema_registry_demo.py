"""Schema registry demo: SchemaRegistry, register_schema, SchemaIDField, LengthField.

This example models a simple sensor-network protocol where a hub receives packets
from several sensor types. Each packet type is a DataList registered in a shared
SchemaRegistry. The registry drives both runtime dispatch and C++ code generation.

Packet wire format (schema-ID only header):

    Header  [ DataUnionHdr: schema_id (16-bit) ]
    Payload [ sensor-specific fields            ]

Because all payload types are fixed-size DataLists, the receiver can recover
the payload word count with ``schema_cls.nwords_per_inst(word_bw)`` — no
explicit length field is needed in the header.
"""
from __future__ import annotations

import math

import numpy as np

from waveflow.hw.dataschema import DataList, FloatField, IntField
from waveflow.hw.dataunion import (
    DataUnionHdr,
    LengthField,
    SchemaIDField,
    SchemaRegistry,
    register_schema,
)


# ---------------------------------------------------------------------------
# 1. Named integer / float types  (Int16 / UInt16 convention)
# ---------------------------------------------------------------------------

UInt8   = IntField.specialize(bitwidth=8,  signed=False)
UInt16  = IntField.specialize(bitwidth=16, signed=False)
Int16   = IntField.specialize(bitwidth=16, signed=True)
Float32 = FloatField.specialize(bitwidth=32)


# ---------------------------------------------------------------------------
# 2. Create the registry
# ---------------------------------------------------------------------------

sensor_reg = SchemaRegistry("Sensor")


# ---------------------------------------------------------------------------
# 3. Register packet types
#    First two use explicit IDs to reserve slots for legacy messages.
#    The rest use auto-assignment so new types need no manual bookkeeping.
# ---------------------------------------------------------------------------

@register_schema(schema_id=1, registry=sensor_reg)
class TemperaturePacket(DataList):
    elements = {
        "temp_raw":  Int16,
        "sensor_id": UInt8,
    }


@register_schema(schema_id=2, registry=sensor_reg)
class PressurePacket(DataList):
    elements = {
        "pressure_pa": UInt16,
        "sensor_id":   UInt8,
    }


@register_schema(registry=sensor_reg)   # auto-assigned: 3
class AccelPacket(DataList):
    elements = {
        "ax": Int16,
        "ay": Int16,
        "az": Int16,
    }


@register_schema(registry=sensor_reg)   # auto-assigned: 4
class DiagnosticsPacket(DataList):
    elements = {
        "uptime_s":    UInt16,
        "error_flags": UInt8,
    }


# ---------------------------------------------------------------------------
# 4. Build a header using DataUnionHdr.specialize()
#    Variable named after the registry + "SchemaID" to mirror the C++ enum type.
# ---------------------------------------------------------------------------

SensorSchemaID = SchemaIDField.specialize(registry=sensor_reg, bitwidth=16)

# Schema-ID only — nwords is derivable from the registry for fixed-size schemas.
PacketHeader = DataUnionHdr.specialize(schema_id_type=SensorSchemaID)

# Alternatively, include an explicit nwords field for streaming / dynamic schemas:
#   Length16 = LengthField.specialize(bitwidth=16)
#   PacketHeaderWithLen = DataUnionHdr.specialize(SensorSchemaID, Length16)


# ---------------------------------------------------------------------------
# 5. Demonstrate registration results
# ---------------------------------------------------------------------------

print("=== Registry contents ===")
for sid, cls in sensor_reg.items():
    print(f"  {sid:2d}  ->  {cls.__name__}")

print()
print(f"next_id()              = {sensor_reg.next_id()}")
print(f"PacketHeader fields    = {list(PacketHeader.elements)}")
print(f"PacketHeader bitwidth  = {PacketHeader.get_bitwidth()} bits")
print(f"PacketHeader @ 32-bit  = {PacketHeader.nwords_per_inst(32)} word(s)")


# ---------------------------------------------------------------------------
# 6. Serialize a packet and its header, then decode on the receive side
# ---------------------------------------------------------------------------

HDR_NWORDS = PacketHeader.nwords_per_inst(word_bw=32)


def encode(schema_id: int, payload: DataList, word_bw: int = 32) -> np.ndarray:
    """Serialize header + payload into a flat word array."""
    hdr = PacketHeader(schema_id=schema_id)
    return np.concatenate([
        hdr.serialize(word_bw=word_bw),
        payload.serialize(word_bw=word_bw),
    ])


def decode(burst: np.ndarray, word_bw: int = 32) -> tuple[int, DataList]:
    """Decode a burst into (schema_id, payload_instance).

    The payload size is recovered from the registry via nwords_per_inst(),
    which is valid for all fixed-size schemas.
    """
    hdr = PacketHeader().deserialize(burst[:HDR_NWORDS], word_bw=word_bw)
    schema_id  = hdr.schema_id
    schema_cls = sensor_reg.get_class(schema_id)
    nwords     = schema_cls.nwords_per_inst(word_bw)
    payload    = schema_cls().deserialize(burst[HDR_NWORDS: HDR_NWORDS + nwords], word_bw=word_bw)
    return schema_id, payload


print()
print("=== Encode / decode round-trip ===")

# Temperature (explicit ID=1)
temp = TemperaturePacket(temp_raw=-42, sensor_id=7)
burst = encode(sensor_reg.get_id(TemperaturePacket), temp)
sid, rx = decode(burst)
print(f"TemperaturePacket  schema_id={sid}  temp_raw={rx.temp_raw}  sensor_id={rx.sensor_id}")
assert sid == 1 and rx.temp_raw == -42 and rx.sensor_id == 7

# Accelerometer (auto-assigned ID=3)
accel = AccelPacket(ax=100, ay=-200, az=980)
burst = encode(sensor_reg.get_id(AccelPacket), accel)
sid, rx = decode(burst)
print(f"AccelPacket        schema_id={sid}  ax={rx.ax}  ay={rx.ay}  az={rx.az}")
assert sid == 3 and rx.ax == 100 and rx.ay == -200 and rx.az == 980

# Diagnostics (auto-assigned ID=4)
diag = DiagnosticsPacket(uptime_s=3600, error_flags=0)
burst = encode(sensor_reg.get_id(DiagnosticsPacket), diag)
sid, rx = decode(burst)
print(f"DiagnosticsPacket  schema_id={sid}  uptime_s={rx.uptime_s}  error_flags={rx.error_flags}")
assert sid == 4 and rx.uptime_s == 3600 and rx.error_flags == 0


# ---------------------------------------------------------------------------
# 7. Show that SchemaIDField rejects unregistered IDs at assignment time
# ---------------------------------------------------------------------------

print()
print("=== SchemaIDField validation ===")

hdr = PacketHeader()
try:
    hdr.schema_id = 99  # not registered
    print("ERROR: should have raised")
except ValueError as exc:
    print(f"Caught expected error: {exc}")


# ---------------------------------------------------------------------------
# 8. Generated C++ enum class
# ---------------------------------------------------------------------------

print()
print("=== Generated C++ enum class ===")
print(SensorSchemaID._gen_include_decl())
