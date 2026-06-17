---
title: Register Maps
parent: Interfaces
nav_order: 4
audience: python
api: [RegMap, RegField, RegAccess, RegMapMMIFSlave, VitisRegMap, VitisRegMapMMIFSlave, BoundRegMap]
summary: "AXI-Lite register maps in the SimPy model — RegField/RegAccess, the RegMap slave dispatch, the VitisRegMap ap_ctrl_hs (ap_start/ap_done) launch lifecycle, and the BoundRegMap host surface."
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

## Host-side: BoundRegMap

Kernel-side `RegMap.get()` / `RegMap.set()` run in-process on the component object. On the host side, you usually have an `MMIFMaster` endpoint plus a base address, so reading and writing fields directly means repeating address arithmetic and schema wrapping at every call site.

`BoundRegMap` provides that host-side convenience surface by binding a `RegMap` instance to a master endpoint:

- `regmap.bind_master(master, base_addr=0) -> BoundRegMap`
- `BoundRegMap.get(name)` (coroutine): reads through `master.read_schema(...)` and returns native Python values (`int`, `IntEnum`, `float`, or schema instances for array/list fields).
- `BoundRegMap.set(name, value)` (coroutine): writes through `master.write_schema(...)`, auto-wrapping raw values using the field schema.
- `BoundRegMap.start()` (coroutine): convenience launch helper for `VitisRegMap` that writes `ap_start`.
- `BoundRegMap.poll_end(field="ap_done", interval=…, max_polls=…)` (coroutine): polls a status field until it reads its completion value (default `ap_done == 1`), returns the read value, and raises after `max_polls`. The standard "wait for the kernel to finish" helper on a `VitisRegMap`.

Source class: [`BoundRegMap`](../../../waveflow/hw/regmap.py).

### Example (host-side testbench)

From [`examples/stream_inband/poly.py`](../../../examples/stream_inband/poly.py), `PolyTB.run_proc`:

```python
rm = self._regmap().bind_master(self.m_lite, base_addr=self.base_addr)

yield from rm.set("coeffs", self.coeffs)
yield from rm.start()

self.halted       = yield from rm.get("halted")
self.error        = yield from rm.get("error")
self.tx_id_status = yield from rm.get("tx_id")
```

This keeps host-side register access aligned with kernel-side ergonomics while preserving typed schema conversions.

### Quick reference

- Use `bind_master(...)` once per `(master, base_addr)` pair.
- `get(name)` returns deserialized typed values.
- `set(name, value)` accepts either schema instances or raw values.
- `start()` / `poll_end()` are available on `VitisRegMap`-backed maps — for the `ap_start` launch and the `ap_done` completion poll.
- `BoundRegMap` is host-side only; kernel logic still uses `RegMap.get/set`.

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

## VitisRegMap

A `VitisRegMap` is a `RegMap` subclass that auto-prepends the standard Vitis HLS `ap_ctrl_hs` control registers. The user only declares their own kernel-specific fields; `ap_start` (W1S, `0x00`) and `ap_done` (R, `0x04`) are added automatically, and the [`VitisRegMapMMIFSlave`](#vitisregmapmmifslave) manages their values (it clears `ap_done` on launch and sets it when the kernel returns).

```python
class VitisRegMap(RegMap):
    """RegMap with Vitis ap_ctrl_hs control conventions auto-applied.

    Prepends `ap_start` (W1S) at 0x00 and `ap_done` (R) at 0x04; user fields
    start at 0x08.  A future v2 packs the remaining control bits (ap_idle,
    ap_ready, auto_restart) into one word plus optional GIE/IER/ISR interrupt
    registers.
    """
    def __init__(self, fields: dict[str, RegField], bitwidth: int = 32) -> None: ...

    def start(self, master: MMIFMaster, base_addr: int = 0) -> ProcessGen[None]:
        """Convenience: host-side launch.  Equivalent to writing 1 to
        `base_addr + offset_of("ap_start")` over the master endpoint."""
```

Use site:

```python
POLY_REGMAP = VitisRegMap({
    "status_clear": RegField(Bit,            RegAccess.W1C, description="Clear halted/error"),
    "halted":       RegField(Bit,            RegAccess.R,   description="1 = halted on error"),
    "error":        RegField(PolyErrorField, RegAccess.R,   description="Last error code"),
    "tx_id":        RegField(TxIdField,      RegAccess.R,   description="TX id of halted txn"),
    "coeffs":       RegField(CoeffArray,     RegAccess.RW,  description="Default coefficients"),
})
# offset_of("ap_start") == 0x00, offset_of("ap_done") == 0x04, offset_of("status_clear") == 0x08, etc.
```

User-declared field names beginning with `ap_` are rejected at construction time to prevent collisions with current and future Vitis-reserved names.

Waveflow's control region is a simplification of Vitis's real control word. Vitis packs `ap_start`, `ap_done`, `ap_idle`, `ap_ready`, and `auto_restart` into one 32-bit register at `0x00` with bit-level access semantics. Waveflow instead exposes `ap_start` and `ap_done` as **two separate full-word registers** (`0x00` and `0x04`) — enough for the launch-then-poll lifecycle, and simpler to model. Host code that writes `1` to `0x00` to launch is bit-compatible with Vitis; the packed `ap_idle` / `ap_ready` / `auto_restart` bits are not yet modeled. See [Planned (v2)](#planned-vitisregmap-v2-control-register).

---

## VitisRegMapMMIFSlave

A `RegMapMMIFSlave` subclass that owns the kernel launch lifecycle. The component author writes the kernel body as an `on_start` generator and registers it with the slave; the slave invokes it as a SimPy process whenever the host writes `ap_start = 1`.

```python
@dataclass
class VitisRegMapMMIFSlave(RegMapMMIFSlave):
    regmap:   VitisRegMap = ...
    on_start: Callable[[], ProcessGen[None]] | None = None
```

### Launch semantics

1. Host writes `1` to the `ap_start` register.
2. If `on_start` is already running (a previous launch hasn't returned), the write is silently ignored. This mirrors Vitis `ap_ctrl_hs`, where `ap_start` writes are gated by `ap_idle`. The W1S auto-clear of `ap_start` still fires.
3. Otherwise the slave clears `ap_done` to `0`, spawns `env.process(on_start())`, and marks itself busy.
4. When `on_start` returns, the slave sets `ap_done` to `1` (in a `finally` block) and marks itself idle. The host polls `ap_done` to detect completion; subsequent `ap_start` writes launch a new invocation.

### What `on_start` should do

`on_start` is the kernel body. It is expected to be a generator that runs until either:

- It reaches an unrecoverable error condition, sets any user-defined status fields via `regmap.set(...)`, and `return`s. The slave will accept subsequent `ap_start` writes once it returns.
- It is intentionally written as a long-running `while True:` loop that processes back-to-back transactions and only returns on error (the **persistent kernel** pattern, which matches the Vitis halt-on-error design we use for poly).

`on_start` must not be invoked from anywhere except the slave's launch path. Component authors do **not** write a `run_proc` for the kernel logic — there is no outer SimPy process waiting on a `start_event`. The slave is the sole entry point.

### What the slave does not do

- The slave **does** auto-manage `ap_done` (cleared on launch, set on return), but does **not** set any *user* status field. Error codes, transaction IDs, sticky flags, etc. are kernel-specific and remain the kernel author's responsibility (set via `regmap.set(name, value)` before `return`ing).
- The slave does **not** model `ap_idle` / `ap_ready` / `auto_restart` as readable registers yet (deferred to v2 — see below).

---

## Worked example: poly accelerator

The polynomial-evaluation kernel from [examples/stream_inband](https://github.com/sdrangan/waveflow/tree/main/examples/stream_inband) uses a `VitisRegMap` for control and status. The kernel implements the **persistent-kernel** pattern: the host writes `ap_start` once, the kernel processes transactions back-to-back from its AXI-Stream input, and only halts (returning) when an error is detected. On halt, the error code and offending transaction ID are latched into the register map for the host to read.

### Field declarations

```python
from enum import IntEnum
from waveflow.hw.dataschema import IntField, EnumField, FloatField, DataArray
from waveflow.hw.regmap import VitisRegMap, RegField, RegAccess

class PolyError(IntEnum):
    NO_ERROR             = 0
    TLAST_EARLY_CMD_HDR  = 1
    NO_TLAST_CMD_HDR     = 2
    TLAST_EARLY_SAMP_IN  = 3
    NO_TLAST_SAMP_IN     = 4
    WRONG_NSAMP          = 5

Bit             = IntField.specialize(bitwidth=1,  signed=False)
TxIdField       = IntField.specialize(bitwidth=16, signed=False)
PolyErrorField  = EnumField.specialize(enum_type=PolyError)
Float32         = FloatField.specialize(bitwidth=32)

class CoeffArray(DataArray):
    ncoeff = 4
    element_type = Float32
    static = True
    max_shape = (ncoeff,)

# Only user-defined fields are declared; ap_start is auto-prepended at 0x00.
POLY_REGMAP_FIELDS = {
    "status_clear": RegField(Bit,            RegAccess.W1C, description="Clear halted/error"),
    "halted":       RegField(Bit,            RegAccess.R,   description="1 = halted on error"),
    "error":        RegField(PolyErrorField, RegAccess.R,   description="Last error code"),
    "tx_id":        RegField(TxIdField,      RegAccess.R,   description="TX id of halted txn"),
    "coeffs":       RegField(CoeffArray,     RegAccess.RW,  description="Default coefficients"),
}
```

### Kernel side

The component declares its endpoints and an `on_start` method. There is **no** `run_proc`, no `start_event`, and no post-construction hook wiring — the slave owns the launch lifecycle.

```python
from waveflow.hw.regmap import VitisRegMap, VitisRegMapMMIFSlave, RegField, RegAccess

@dataclass
class PolyAccelComponent(HwComponent):

    def __post_init__(self) -> None:
        super().__post_init__()
        self.s_in  = StreamIFSlave (name=f'{self.name}_s_in',  sim=self.sim, bitwidth=self.in_bw)
        self.m_out = StreamIFMaster(name=f'{self.name}_m_out', sim=self.sim, bitwidth=self.out_bw)

        # Build a per-instance VitisRegMap with hooks bound to component methods.
        self.regmap = VitisRegMap({
            "status_clear": RegField(Bit, RegAccess.W1C, on_write=self._on_status_clear,
                                     description="Clear halted/error"),
            "halted":       RegField(Bit, RegAccess.R, description="1 = halted on error"),
            "error":        RegField(PolyErrorField, RegAccess.R, description="Last error code"),
            "tx_id":        RegField(TxIdField, RegAccess.R, description="TX id of halted txn"),
            "coeffs":       RegField(CoeffArray, RegAccess.RW, description="Default coefficients"),
        })
        self.s_lite = VitisRegMapMMIFSlave(
            name=f'{self.name}_s_lite', sim=self.sim, bitwidth=32,
            regmap=self.regmap, on_start=self.on_start,
        )
        for ep in (self.s_in, self.m_out, self.s_lite):
            self.add_endpoint(ep)

    def _on_status_clear(self, name, sub_word, value):
        self.regmap.set("halted", 0)
        self.regmap.set("error",  PolyError.NO_ERROR)

    def on_start(self) -> ProcessGen[None]:
        """Kernel body — invoked by VitisRegMapMMIFSlave on host ap_start write."""
        while True:
            cmd_hdr = yield from self.s_in.get(PolyCmdHdr)
            err = yield from self.evaluate(cmd_hdr, self.s_in, self.m_out)
            if err != PolyError.NO_ERROR:
                self.regmap.set("error",  err)
                self.regmap.set("tx_id",  cmd_hdr.tx_id)
                self.regmap.set("halted", 1)
                return         # halt → slave goes idle; host can re-launch via ap_start
```

### Host side

```python
# Configure default coefficients (one LITE transaction per word, auto-split)
yield from cpu.write_schema(CoeffArray([1.0, 0.0, 0.5, 0.25]),
                            addr=POLY_BASE + poly.regmap.offset_of("coeffs"))

# Launch via the VitisRegMap convenience method
yield from poly.regmap.start(cpu, base_addr=POLY_BASE)

# ... time passes; host issues stream transactions on the data path ...

# On suspected halt: poll status
halted = yield from cpu.read_schema(Bit, addr=POLY_BASE + poly.regmap.offset_of("halted"))
if halted:
    err   = yield from cpu.read_schema(PolyErrorField, addr=POLY_BASE + poly.regmap.offset_of("error"))
    tx_id = yield from cpu.read_schema(TxIdField,      addr=POLY_BASE + poly.regmap.offset_of("tx_id"))
    log.error(f"poly halted on tx {tx_id}: {err}")
    yield from cpu.write_schema(Bit(1), addr=POLY_BASE + poly.regmap.offset_of("status_clear"))
    yield from poly.regmap.start(cpu, base_addr=POLY_BASE)        # re-launch
```

The same `VitisRegMap` object drives the SimPy simulation, the (planned) HLS pragma generation, and the (planned) host driver class — see below.

---

## Planned: VitisRegMap v2 control register

Today `VitisRegMap` exposes `ap_start` (`0x00`) and `ap_done` (`0x04`) as two separate full-word registers — enough for the launch-then-poll lifecycle. v2 would pack them, plus the remaining control bits, into the single bit-packed `ap_ctrl_hs` register at `0x00` that Vitis HLS actually generates, with bit-level access semantics within one bus word.

### Bit layout at offset 0x00

| Bit | Name           | Access | Notes                                         |
|-----|----------------|--------|-----------------------------------------------|
| 0   | `ap_start`     | W1S    | auto-clears when slave begins running on_start |
| 1   | `ap_done`      | COR    | set when on_start returns; cleared on host read |
| 2   | `ap_idle`      | R      | 1 when on_start is not running                |
| 3   | `ap_ready`     | R      | 1 when ready to accept the next ap_start      |
| 7   | `auto_restart` | RW     | when 1, slave re-invokes on_start immediately on return |

Three new infrastructure pieces are needed to support this:

- **`RegAccess.COR`** (clear-on-read): host reads return the current value, then the backing store is zeroed.
- **`BitField` / packed-field support in `RegField`**: multiple named bit fields at the same byte offset, each with its own access mode. `RegField.bits = {"ap_start": 0, "ap_done": 1, ...}` or a parallel declaration syntax.
- **`auto_restart` semantics in `VitisRegMapMMIFSlave`**: when the bit is set and `on_start` returns, the slave immediately re-invokes `on_start` without requiring another host write.

### Optional interrupt registers

A `VitisRegMap(..., interrupts=True)` flag adds the standard Vitis interrupt registers:

| Offset | Name | Width | Description                          |
|--------|------|-------|--------------------------------------|
| 0x04   | GIE  | 1     | Global interrupt enable              |
| 0x08   | IER  | 2     | Interrupt enable for ap_done, ap_ready |
| 0x0C   | ISR  | 2     | Interrupt status (W1C)               |

When `ap_done` asserts and the corresponding IER bit is set, the slave fires an `interrupt_event` (a SimPy event) that the host model can `yield` on instead of polling.

### Host-side accessors

The auto-generated driver class gets per-bit accessors mirroring Vitis's generated `xpoly.h`:

```python
drv.start()                # write 1 to ap_start
drv.is_done()              # read ap_done (clears it)
drv.is_idle()              # read ap_idle
drv.set_auto_restart(True) # write bit 7 of control
drv.enable_interrupts()    # write GIE, IER
yield from drv.wait_interrupt()   # yield on the slave's interrupt_event
```

User code that targets v1 (writes `1` to `0x00` to launch) continues to work in v2 — the only change is that more bits at `0x00` become readable and the control register loses its "single ap_start bit" simplification.

---

## Planned: artifact generation (v2)

The register map is declarative Python data, so it can drive generation of host-side artifacts. The following are designed-for but **not yet implemented in v1**. Names and signatures are specified here so the generators can be added without breaking changes.

### Markdown table

```python
def to_markdown(self, *, title: str | None = None) -> str
```

Renders a table suitable for inclusion in design docs:

```markdown
### POLY register map

| Offset | Name         | Access | Width | Description                  |
|--------|--------------|--------|-------|------------------------------|
| 0x00   | ap_start     | W1S    | 1     | Start kernel                 |
| 0x04   | status_clear | W1C    | 1     | Clear halted/error           |
| 0x08   | halted       | R      | 1     | 1 = halted on error          |
| 0x0C   | error        | R      | 8     | Last error code              |
| 0x10   | tx_id        | R      | 16    | TX id of halted txn          |
| 0x14   | coeffs[4]    | RW     | 4×32  | Default coefficients         |
```

### C header

```python
def to_c_header(self, *, prefix: str) -> str
```

Generates `#define`s for offsets and bit widths, plus a packed struct for composite fields:

```c
/* Auto-generated from POLY_REGMAP — do not edit. */
#define POLY_AP_START_OFFSET     0x00u
#define POLY_STATUS_CLEAR_OFFSET 0x04u
#define POLY_HALTED_OFFSET       0x08u
#define POLY_ERROR_OFFSET        0x0Cu
#define POLY_TX_ID_OFFSET        0x10u
#define POLY_COEFFS_OFFSET       0x14u
#define POLY_COEFFS_COUNT        4u
```

### Python driver class

```python
def to_python_driver(self, *, class_name: str) -> str
```

Generates a class that wraps an `MMIFMaster` with one accessor per field, returning deserialized Python values:

```python
class PolyDriver:
    def __init__(self, master: MMIFMaster, base_addr: int) -> None: ...

    def write_ap_start(self) -> ProcessGen[None]: ...
    def write_status_clear(self) -> ProcessGen[None]: ...

    def read_halted(self) -> ProcessGen[bool]: ...
    def read_error(self)  -> ProcessGen[PolyError]: ...
    def read_tx_id(self)  -> ProcessGen[int]: ...

    def write_coeffs(self, value: CoeffArray | list[float]) -> ProcessGen[None]: ...
    def read_coeffs(self) -> ProcessGen[CoeffArray]: ...
```

The driver is the single touchpoint for host-side firmware and software-in-the-loop tests. Because the same `RegMap` object also drives the simulation and the (eventual) HLS pragma generation, the offsets cannot drift between the three.

---

## Quick reference

```python
from waveflow.hw.regmap import (
    RegMap, RegField, RegAccess, RegMapMMIFSlave,
    VitisRegMap, VitisRegMapMMIFSlave,
)
```

| Operation | Code |
|---|---|
| Declare a field             | `RegField(SchemaType, RegAccess.RW, description="…", on_write=cb)` |
| Declare a generic regmap    | `RegMap({"name": RegField(...), ...}, bitwidth=32)` |
| Declare a Vitis regmap      | `VitisRegMap({"name": RegField(...), ...})` |
| Look up offset              | `regmap.offset_of("name")` |
| Total size in bytes         | `regmap.total_size_bytes()` |
| Owner-side write            | `regmap.set("error", PolyError.NO_TLAST)` |
| Owner-side read             | `regmap.get("coeffs")` |
| Create generic slave        | `RegMapMMIFSlave(sim=sim, bitwidth=32, regmap=regmap)` |
| Create Vitis slave          | `VitisRegMapMMIFSlave(sim=sim, bitwidth=32, regmap=regmap, on_start=self.on_start)` |
| Bind to crossbar            | `xbar.bind("slave_0", slave_ep, protocol=AXIMMProtocol.LITE)` |
| Bind direct                 | `direct.bind("slave",  slave_ep)` |
| Host write a field          | `yield from master.write_schema(value, addr=base + regmap.offset_of("name"))` |
| Host read a field           | `val = yield from master.read_schema(SchemaType, addr=base + regmap.offset_of("name"))` |
| Host launch a Vitis kernel  | `yield from regmap.start(master, base_addr=BASE)` |

## Worked example

For an end-to-end walkthrough that puts these abstractions to work — declaring a `VitisRegMap`, running it in SimPy, generating the Vitis HLS kernel, and validating the measured RTL timing against the Python model — see the [Register Map example](../../examples/regmap/) in the Examples section.
