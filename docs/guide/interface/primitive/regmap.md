---
title: Register Maps
parent: Primitive interfaces
grand_parent: Interfaces
nav_order: 4
audience: python
api: [RegMap, RegField, RegAccess, RegMapMMIFSlave, MMIFSlave, DataSchema]
summary: "The AXI-Lite register map as an interface — RegField / RegAccess (R, W, RW, W1C, W1S), the auto-assigned offset table, the RegMapMMIFSlave read/write dispatch, composite and bit-packed fields, and the per-transaction hook contract. axilite_slave is its own kind_of_endpoint boundary kind. The launch lifecycle layered on top of it — VitisRegMap's ap_ctrl_hs and the BoundRegMap host surface — is a separate page."
---

# Register Maps

A **register map** is the conventional way to expose a small block of named, individually-addressable control and status fields to a host over AXI-Lite. Each named field gets its own bus offset, so the host can read or write one field at a time without paying for the others.

Waveflow provides the register-map abstraction as a thin layer on top of the existing [MM interfaces](./aximm.md). The slave endpoint is an `MMIFSlave` that is wired by the framework — the component author declares a `RegMap`, and the slave's read/write callbacks dispatch to fields automatically.

| Class | Role |
|---|---|
| `RegAccess` | Enum of access modes (`R`, `W`, `RW`, `W1C`, `W1S`) |
| `RegField` | Declaration of one field: schema, access mode, hooks |
| `RegMap` | Ordered collection of `RegField`s; owns the backing values |
| `RegMapMMIFSlave` | Subclass of `MMIFSlave` that dispatches reads/writes to a `RegMap` |

The register map matches the model that Vitis HLS generates from `s_axilite` scalars and arrays: each field becomes one or more 32-bit registers in a single auto-generated AXI-Lite slave. When Waveflow eventually generates the HLS pragmas for a kernel, the offsets in the Python `RegMap` are the offsets the host driver uses.

---


> **This page is the interface.** The fields, the offsets, the bus dispatch — everything that makes
> a register map an AXI-Lite slave, and `axilite_slave` a `kind_of_endpoint` boundary kind. The
> **launch lifecycle** built on top of it — `VitisRegMap`'s `ap_ctrl_hs` control block, the
> `ap_start` / `ap_done` handshake, `VitisRegMapMMIFSlave`, and the `BoundRegMap` host surface —
> is [Host launch lifecycle](../../comp_codegen/host_launch.md). A register map is useful without a
> launch; a launch is not possible without a register map, which is the direction of the dependency.

## Quick example

```python
from enum import IntEnum
from waveflow.hw.aximm import AXIMMCrossBarIF, AXIMMProtocol, MMIFMaster
from waveflow.hw.dataschema import EnumField, IntField
from waveflow.hw.regmap import RegMap, RegField, RegAccess, RegMapMMIFSlave

class ErrorCode(IntEnum):
    OK           = 0
    BAD_FRAMING  = 1
    WRONG_LENGTH = 2

Bit            = IntField.specialize(bitwidth=1, signed=False)
ErrorCodeField = EnumField.specialize(enum_type=ErrorCode)

regmap = RegMap({
    "ap_start":  RegField(Bit,            RegAccess.W1S, description="Start the kernel"),
    "halted":    RegField(Bit,            RegAccess.R,   description="1 = halted on error"),
    "error":     RegField(ErrorCodeField, RegAccess.R,   description="Last error code"),
})

slave_ep = RegMapMMIFSlave(sim=sim, bitwidth=32, regmap=regmap)
xbar.bind("slave_0", slave_ep, protocol=AXIMMProtocol.LITE)
```

The host then reads or writes each field at its own auto-assigned offset:

```python
yield from cpu_master.write_schema(Bit(1), addr=regmap.offset_of("ap_start"))
err = yield from cpu_master.read_schema(ErrorCodeField, addr=regmap.offset_of("error"))
```

---

## RegField

```python
@dataclass
class RegField:
    schema:      type[DataSchema]
    access:      RegAccess
    description: str = ""
    on_write:    Callable[[str, int, int], None] | None = None
    on_read:     Callable[[str, int, int], None] | None = None
    offset:      int | None = None         # None = auto-assign
```

- `schema` — any `DataSchema` subclass: `IntField`, `EnumField`, `FloatField`, `DataList`, `DataArray`. The field occupies `schema.nwords_per_inst(bus_bw)` consecutive bus words.
- `access` — one of `RegAccess.R`, `W`, `RW`, `W1C`, `W1S` (see below).
- `description` — free-text; included in generated documentation.
- `on_write`, `on_read` — hook callbacks; see [Hooks](#hooks).
- `offset` — optional manual byte offset within the slave's address range. When `None`, the offset is auto-assigned in declaration order.

**Validation rules** (checked at `RegMap` construction):

- `W1C` and `W1S` require single-word scalar fields (`schema.nwords_per_inst(bus_bw) == 1`); these modes are bit-level semantics that are not meaningful for multi-word fields.
- All offsets must be aligned to the bus word size (`bus_bw / 8` bytes).
- No two fields' word ranges may overlap.

---

## RegAccess

| Mode | Host read | Host write | Owner read | Owner write |
|---|---|---|---|---|
| `R`   | OK    | rejected | OK | OK |
| `W`   | rejected | OK    | OK | OK |
| `RW`  | OK    | OK    | OK | OK |
| `W1C` | OK    | OK (bits set in the written value clear the corresponding bits in the backing store) | OK | OK |
| `W1S` | OK    | OK (bits set in the written value set the bit, the hook fires, then the bit auto-clears to 0) | OK | OK |

Rejected host operations raise `RegMapAccessError` (caught and logged by the slave; the bus transaction completes returning 0 for the read path).

### W1S (write-1-to-set, auto-clearing)

Models trigger registers like `ap_start`. The sequence on a host write of `1`:

1. Backing word is set to `1`.
2. `on_write(name, 0, 1)` hook fires. The hook should `succeed()` a SimPy event, increment a counter, etc.
3. Backing word is set back to `0`.

Subsequent host reads return `0` until another host write of `1` re-triggers the cycle. The hook always sees the value `1` during its invocation.

### W1C (write-1-to-clear)

Models sticky status bits. On a host write of value `v`:

1. Backing word is updated as `backing &= ~v` (each bit set in `v` clears the corresponding bit in the backing store).
2. `on_write(name, 0, v)` fires.

Owner-side `regmap.set(name, value)` does **not** apply W1C semantics — owner writes overwrite the backing store directly. This matches how a kernel writes its sticky-status registers (set on event, host clears).

---

## RegMap

```python
class RegMap:
    def __init__(self, fields: dict[str, RegField], bitwidth: int = 32) -> None: ...

    # Layout
    def offset_of(self, name: str) -> int
    def nwords_of(self, name: str) -> int
    def total_size_bytes(self) -> int

    # Owner-side value access (deserialized form)
    def get(self, name: str) -> Any
    def set(self, name: str, value: Any) -> None
```

### Offset assignment

Fields are auto-assigned offsets in declaration order, packed tightly with bus-word alignment:

```python
RegMap({
    "ap_start": RegField(Bit,        RegAccess.W1S),  # 1 word  → offset 0x00
    "halted":   RegField(Bit,        RegAccess.R),    # 1 word  → offset 0x04
    "coeffs":   RegField(CoeffArray, RegAccess.RW),   # 4 words → offset 0x08, 0x0C, 0x10, 0x14
    "error":    RegField(ErrorCode,  RegAccess.R),    # 1 word  → offset 0x18
})
```

Manual override per field:

```python
RegMap({
    "control": RegField(ControlReg, RegAccess.RW, offset=0x00),
    "status":  RegField(StatusReg,  RegAccess.R,  offset=0x40),
})
```

Manually-placed fields establish fixed positions; auto-placed fields fill the gaps in declaration order. Overlap raises `ValueError` at construction.

### Owner-side API

The owning component reads and writes fields using the deserialized Python value, not raw words:

```python
self.regmap.set("error",  PolyError.TLAST_EARLY_CMD_HDR)
self.regmap.set("tx_id",  cmd_hdr.tx_id)
self.regmap.set("halted", 1)

current_coeffs = self.regmap.get("coeffs")     # DataArray of Float32
```

Internally each field's backing store is a numpy array of `nwords_per_inst(bus_bw)` words. `get()` calls `schema().deserialize(buffer)`; `set()` calls `value.serialize()` (or wraps a raw value via `schema(value)` first) and stores. Host bus reads/writes touch the same underlying word buffer at the appropriate sub-word offset.

---

## RegMapMMIFSlave

```python
@dataclass
class RegMapMMIFSlave(MMIFSlave):
    regmap: RegMap = ...
```

A subclass of [`MMIFSlave`](./aximm.md#mmifslave) that wires its own `rx_read_proc` and `rx_write_proc`:

- Decodes `local_addr` to `(field_name, sub_word_index)` against the `RegMap`'s offset table.
- For LITE crossbar binds, each callback receives one word at a time. Reads return the appropriate slice of the field's backing word buffer; writes update the buffer (applying access-mode rules) and fire the hook.
- For FULL binds (a register file connected via FULL is unusual but supported), multi-word transfers are decoded contiguously, one field at a time.
- Out-of-range or unaligned addresses raise `RegMapAccessError`.

The slave is bound exactly like any other `MMIFSlave`:

```python
xbar.bind("slave_0", regmap_slave, protocol=AXIMMProtocol.LITE)
assign_address_ranges([regmap_slave], [(0x4000, regmap.total_size_bytes())])
```

Or via `DirectMMIF` for a single-master/single-slave register file:

```python
direct = DirectMMIF(sim=sim, clk=clk, byte_addressable=True)
direct.bind("master", host_master)
direct.bind("slave",  regmap_slave)
```

---

## Composite fields

Any `DataSchema` may be used as a field. Multi-word schemas occupy consecutive bus-word offsets; the host accesses individual words via LITE transactions, while the owner sees the deserialized value as a single Python object.

```python
class CoeffArray(DataArray):
    ncoeff = 4
    element_type = Float32
    static = True
    max_shape = (ncoeff,)

regmap = RegMap({
    "coeffs": RegField(CoeffArray, RegAccess.RW),
})

# Owner: writes/reads the whole array as one schema instance
self.regmap.set("coeffs", CoeffArray([1.0, 0.0, 0.5, 0.25]))
arr = self.regmap.get("coeffs")           # DataArray of Float32, length 4

# Host: reads element 2 (one LITE transaction at offset 0x08)
word2 = yield from master.read_schema(Float32, addr=regmap.offset_of("coeffs") + 0x08)
```

This matches Vitis HLS behavior for `s_axilite` arrays and structs: the host sees `nwords_per_inst` consecutive registers, and the kernel sees the field as a single typed object.

---

## Hooks

Hook callbacks fire **per host bus transaction**, not per logical field write. AXI-Lite has no notion of a "field write complete" — the host writes one word at a time — so the hook contract is per-word.

```python
on_write(name: str, sub_word: int, word_value: int) -> None
on_read (name: str, sub_word: int, word_value: int) -> None
```

- `name` — the field's declared name in the `RegMap`.
- `sub_word` — index of the word within the field (always `0` for single-word scalars).
- `word_value` — for `on_write`, the raw word value the host wrote (before any access-mode transformation); for `on_read`, the value about to be returned.

Ordering:

- `on_write` fires **after** the backing store update (after W1C masking) and **before** the W1S auto-clear. Hooks reading `regmap.get(name)` see the just-written value.
- `on_read` fires **after** the value is read from the backing store, **before** it is returned on the bus.

Hooks must not yield. To gate a SimPy generator on a host write (the typical `ap_start` pattern), have the hook `succeed()` an event that another `run_proc` is `yield`-ing on:

```python
self._start_event = self.env.event()

def _on_ap_start(name, sub_word, value):
    self._start_event.succeed()
    self._start_event = self.env.event()

regmap = RegMap({
    "ap_start": RegField(Bit, RegAccess.W1S, on_write=_on_ap_start),
    ...
})
```

For composite fields, callers that need "all words written" semantics must track that themselves (e.g., by maintaining a bitmask of which sub-words have been written since the last reset).

---

## Bit-packed fields

Several fields can share one bus word. A field declares `bit_offset` — the position of its LSB within the word at `offset` — and then occupies just that bit range:

```python
RegMap({
    "ap_start": RegField(Bit, RegAccess.W1S, offset=0x00, bit_offset=0),
    "ap_done":  RegField(Bit, RegAccess.R,   offset=0x00, bit_offset=1),
})
```

This is the mechanism [`VitisRegMap`](../../comp_codegen/host_launch.md#vitisregmap) uses for the `0x00` control word. Rules:

- A packed field must name its `offset` explicitly, and its schema must be a scalar with a `bitwidth` that fits in the word. Overlapping bit ranges raise `ValueError` at construction.
- Each packed field still gets **its own backing buffer holding the unshifted value**, so owner-side `get()` / `set()` are identical for packed and unpacked fields. Only the bus-level word composition differs.
- `field_name_at_offset()` raises on a packed word — it has no single owning field. Use `bit_fields_at_offset(offset)` to list the fields sharing it, and `bit_offset_of(name)` to get one field's position.

Layout queries:

```python
rm.bit_offset_of("ap_done")       # 1  (None for an unpacked field)
rm.bit_fields_at_offset(0x00)     # ["ap_start", "ap_done", "ap_idle", "ap_ready"]
```

### Bus semantics

Packing costs no extra transactions. `BoundRegMap.get(name)` reads the containing word and extracts the field's bits; `set(name, value)` composes a word with the field's bits in place and zeros elsewhere, then issues **one** write — there is no read-modify-write.

That mirrors the hardware, where a word write drives every writable bit of the word, and it has two consequences:

- **Read-only bits ignore writes rather than raising.** A packed word mixes `R` and writable bits, so writing `ap_start` cannot be an access violation just because read-only `ap_done` sits beside it. The hardware slave likewise decodes only the writable bits.
- **Writing one field of a word holding *several writable* fields zeroes the others** — exactly as it would on the bus. A caller that needs to preserve a neighbour must compose the word itself. This does not bite the Vitis control word, where `ap_start` is the only writable bit.

What this model does *not* reproduce — `RegAccess.COR`, `auto_restart`, interrupts, and the
missing `control.h` conformance test — is listed on
[Host launch lifecycle](../../comp_codegen/host_launch.md#not-yet-modelled), with the Vitis
control block those limits are about.

---

## Quick reference

```python
from waveflow.hw.regmap import RegMap, RegField, RegAccess, RegMapMMIFSlave
```

| Operation | Code |
|---|---|
| Declare a field             | `RegField(SchemaType, RegAccess.RW, description="…", on_write=cb)` |
| Declare a generic regmap    | `RegMap({"name": RegField(...), ...}, bitwidth=32)` |
| Look up offset              | `regmap.offset_of("name")` |
| Total size in bytes         | `regmap.total_size_bytes()` |
| Owner-side write            | `regmap.set("error", PolyError.NO_TLAST)` |
| Owner-side read             | `regmap.get("coeffs")` |
| Create generic slave        | `RegMapMMIFSlave(sim=sim, bitwidth=32, regmap=regmap)` |
| Bind to crossbar            | `xbar.bind("slave_0", slave_ep, protocol=AXIMMProtocol.LITE)` |
| Bind direct                 | `direct.bind("slave",  slave_ep)` |
| Host write a field          | `yield from master.write_schema(value, addr=base + regmap.offset_of("name"))` |
| Host read a field           | `val = yield from master.read_schema(SchemaType, addr=base + regmap.offset_of("name"))` |

The `VitisRegMap` rows of this table are on
[Host launch lifecycle](../../comp_codegen/host_launch.md#vitis-quick-reference).

## Worked example

The end-to-end walkthrough — declaring a `VitisRegMap`, running it in SimPy, generating the Vitis
HLS kernel and validating the measured RTL timing — is the
[Register Map example](../../../examples/regmap/), reached from
[Host launch lifecycle](../../comp_codegen/host_launch.md#worked-example).
