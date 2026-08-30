---
title: Host launch lifecycle
parent: Module Code Generation
nav_order: 3.5
audience: python
api: [VitisRegMap, VitisRegMapMMIFSlave, BoundRegMap, MMIFMaster, SimObj, Simulation]
summary: "How a host starts a HostActivated kernel and learns that it finished, modelled in SimPy — VitisRegMap's ap_ctrl_hs control block (the 0x00 word, the 0x10 user-field base), the ap_start / ap_done handshake VitisRegMapMMIFSlave runs around on_start, and the BoundRegMap host surface (bind_master / start / poll_end). Includes what the model does not reproduce about real ap_ctrl_hs, and the planned host-artifact generators."
---

# Host launch lifecycle

A [`HostActivated`](../flows/modules.md) module does not run until a host starts it. That handshake
is `ap_ctrl_hs`: the host writes `ap_start`, the kernel runs, the kernel raises `ap_done`, the host
polls until it sees it. This page is the **Python model** of that lifecycle — what the simulation
does, so that the [generated kernel](./hostactivated.md) and the simulation agree about when work
begins and ends.

It sits on top of the register map, and only on top of it: the fields, the offsets and the AXI-Lite
dispatch underneath are [Register Maps](../interface/primitive/regmap.md). A register map is useful
without a launch; a launch is not possible without a register map.

## A minimal simulation

Two raw [`SimObj`](../sim/simobj.md)s exercising the launch-then-poll lifecycle over a
[`DirectMMIF`](../interface/primitive/aximm.md#directmmif): a `Kernel` holding a `VitisRegMapMMIFSlave` runs its `on_start`
when launched, and a `Host` holding an `MMIFMaster` writes the inputs, asserts `ap_start`, polls
`ap_done`, and reads the result back. No `HwModule`. (`on_start` is the regmap-launched entry — see
the [SimObj lifecycle](../sim/simobj.md#its-lifecycle); the `yield from` mechanics are in
[Process generators](../sim/procgen.md).)

```python
from dataclasses import dataclass

from waveflow.hw.aximm import DirectMMIF, MMIFMaster
from waveflow.hw.clock import Clock
from waveflow.hw.dataschema import IntField
from waveflow.hw.regmap import RegAccess, RegField, VitisRegMap, VitisRegMapMMIFSlave
from waveflow.simulation.simobj import ProcessGen, SimObj
from waveflow.simulation.simulation import Simulation

Int32 = IntField.specialize(bitwidth=32, signed=True)


@dataclass
class Kernel(SimObj):
    """A regmap-launched compute SimObj: y = a*x + b, run on host ap_start."""

    clk: Clock | None = None

    def __post_init__(self) -> None:
        super().__post_init__()
        self.regmap = VitisRegMap({
            "x": RegField(Int32, RegAccess.RW),
            "a": RegField(Int32, RegAccess.RW),
            "b": RegField(Int32, RegAccess.RW),
            "y": RegField(Int32, RegAccess.R),
        })
        self.s_lite = VitisRegMapMMIFSlave(
            name=f"{self.name}_s_lite", sim=self.sim, bitwidth=32,
            regmap=self.regmap, on_start=self.on_start,
        )

    def on_start(self) -> ProcessGen[None]:
        # The slave invokes this on ap_start; it auto-sets ap_done when on_start returns.
        x = int(self.regmap.get("x").val)
        a = int(self.regmap.get("a").val)
        b = int(self.regmap.get("b").val)
        yield self.timeout(4 * self.clk.period)          # model compute latency
        self.regmap.set("y", a * x + b)


@dataclass
class Host(SimObj):
    """Holds the master; configures inputs, launches, polls ap_done, reads y back."""

    master: MMIFMaster | None = None
    kernel: Kernel | None = None
    clk: Clock | None = None

    def __post_init__(self) -> None:
        super().__post_init__()
        self.y: int | None = None

    def run_proc(self) -> ProcessGen[None]:
        rm = self.kernel.regmap.bind_master(self.master, base_addr=0)
        yield from rm.set("x", 5)
        yield from rm.set("a", 3)
        yield from rm.set("b", -4)
        yield from rm.start()                            # write ap_start
        ap_done = yield from rm.poll_end(interval=4 * self.clk.period, max_polls=32)
        self.y = yield from rm.get("y")
        print(f"ap_done={ap_done}, y={self.y}")


sim = Simulation()
clk = Clock(freq=100e6)

kernel = Kernel(name="kernel", sim=sim, clk=clk)
host = Host(name="host", sim=sim, master=MMIFMaster(sim=sim, bitwidth=32), kernel=kernel, clk=clk)

link = DirectMMIF(sim=sim, clk=clk, byte_addressable=True)   # byte addresses (AXI-Lite convention)
link.bind("master", host.master)
link.bind("slave", kernel.s_lite)

sim.run_sim()
```

`host.y` is `11` (`3*5 - 4`): the host's `set` writes land in the register fields, `start()` writes
`ap_start` which launches `on_start`, the slave sets `ap_done` when it returns, and `poll_end` reads
that back before the host fetches `y`. `bind_master` / `start` / `poll_end` are the host-side
[`BoundRegMap`](#host-side-boundregmap) surface. See [SimObj](../sim/simobj.md) for the lifecycle.

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

## VitisRegMap

A `VitisRegMap` is a `RegMap` subclass that reproduces the s_axilite control layout Vitis HLS generates. The user only declares their own kernel-specific fields; the control block is added automatically, and the [`VitisRegMapMMIFSlave`](#vitisregmapmmifslave) manages the control bits (it clears `ap_done` on launch and sets it when the kernel returns).

The control block occupies the first 16 bytes, and **user fields start at `0x10` on an 8-byte stride** — Vitis gives each 32-bit scalar argument a data word plus a control/reserved word:

| Offset | Contents |
|--------|----------|
| `0x00` | Control word — `ap_start` (bit 0), `ap_done` (bit 1), `ap_idle` (bit 2), `ap_ready` (bit 3) |
| `0x04` | `gier` — Global Interrupt Enable Register |
| `0x08` | `ier` — IP Interrupt Enable Register |
| `0x0C` | `isr` — IP Interrupt Status Register |
| `0x10` | first user field (`0x14` reserved), then `0x18`, `0x20`, … |

The four `ap_*` signals are **bits of one word**, not registers of their own. See [Bit-packed fields](../interface/primitive/regmap.md#bit-packed-fields) for the mechanism, and [Fidelity](#fidelity-what-is-and-is-not-modelled) for what the model does not reproduce.

```python
class VitisRegMap(RegMap):
    """RegMap mirroring the s_axilite control layout Vitis HLS generates."""
    def __init__(self, fields: dict[str, RegField], bitwidth: int = 32) -> None: ...

    def start(self, master: MMIFMaster, base_addr: int = 0) -> ProcessGen[None]:
        """Convenience: host-side launch.  Writes 1 to bit 0 of the control
        word at `base_addr` over the master endpoint."""
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
# offset_of("ap_start") == 0x00 with bit_offset_of("ap_start") == 0
# offset_of("ap_done")  == 0x00 with bit_offset_of("ap_done")  == 1
# offset_of("status_clear") == 0x10, offset_of("halted") == 0x18, etc.
```

`VitisRegMap` requires `bitwidth=32` — the Vitis s_axilite control bus is 32 bits wide and the control block is defined on 4-byte words.

User-declared field names beginning with `ap_` are rejected at construction time to prevent collisions with current and future Vitis-reserved names, as are manual offsets inside the reserved `0x00`–`0x0f` control block.

### Fidelity: what is and is not modelled

The *layout* mirrors Vitis. The *side effects* are modelled only as far as the simulator needs:

- **`ap_done` / `ap_ready` are not clear-on-read.** Real hardware clears them when the host reads `0x00` (`COR`). The model clears them on the next `ap_start` instead, so a host can read `ap_done` repeatedly and keep seeing `1`.
- **`ap_start` is `W1S`, not `COH`.** It auto-clears once the launch hook has run rather than on the `ap_ready` handshake — the same net effect for a sim that launches synchronously.
- **`gier` / `ier` / `isr` are plain storage.** There is no interrupt line in the simulation; writing them enables nothing, and `isr` does not implement toggle-on-write.
- **`auto_restart` (bit 7) and `interrupt` (bit 9) are not modelled** at all.
- **The multi-word stride is unverified.** The 8-byte stride is confirmed for 32-bit scalars. Fields spanning several words follow the same data-words-plus-control-word rule, but Vitis maps array arguments on s_axilite as a BRAM-backed region, which `VitisRegMap` does not reproduce.

Nothing currently *enforces* that this layout tracks Vitis. The offsets are not shared with the kernel — codegen emits no addresses, and Vitis assigns them from the `s_axilite` pragmas — so the Python table is used only inside the simulation, by both `BoundRegMap` and the slave. The authoritative artifact is the `control.h` that Vitis writes beside the generated RTL (`<proj>/solution1/.autopilot/db/coregen/control.h`); a build step that parses it and diffs it against `VitisRegMap` would turn today's mirror into a checked contract. That conformance test is follow-on work.

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

- The slave **does** auto-manage `ap_done` / `ap_ready` / `ap_idle` (cleared on launch, set on return), but does **not** set any *user* status field. Error codes, transaction IDs, sticky flags, etc. are kernel-specific and remain the kernel author's responsibility (set via `regmap.set(name, value)` before `return`ing).
- The slave does **not** clear `ap_done` / `ap_ready` on read, and does **not** model `auto_restart` — see [Not yet modelled](#not-yet-modelled).

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

# Only user-defined fields are declared; the Vitis control block (0x00-0x0f)
# is added automatically, so these land from 0x10 up.
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
class PolyAccel(HwModule):

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
            cmd_hdr = yield from self.s_in.get_schema(PolyCmdHdr)
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

The same `VitisRegMap` object drives the SimPy simulation and would drive the (planned) HLS pragma generation and host driver class — see below. Note that this is a single *declaration* of the fields, not a single source of the offsets: codegen emits no addresses, and Vitis assigns them from the `s_axilite` pragmas. `VitisRegMap` mirrors the layout Vitis documents; nothing yet checks the mirror — see [Fidelity](#fidelity-what-is-and-is-not-modelled).

---

## Not yet modelled

- **`RegAccess.COR`** (clear-on-read): host reads return the current value, then the backing store is zeroed. Real `ap_done` / `ap_ready` are `COR`; the model clears them on the next `ap_start` instead.
- **`auto_restart` semantics in `VitisRegMapMMIFSlave`**: when bit 7 is set and `on_start` returns, the slave would immediately re-invoke `on_start` without another host write.
- **Interrupts.** `gier` / `ier` / `isr` exist as storage only. Wiring them up would mean firing an `interrupt_event` (a SimPy event) when `ap_done` asserts with the matching `ier` bit set, so a host model could `yield` on it instead of polling.
- **A `control.h` conformance test.** Nothing checks the modelled layout against the artifact Vitis emits — see [Fidelity](#fidelity-what-is-and-is-not-modelled).

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

| Offset | Bit | Name         | Access | Width | Description                  |
|--------|-----|--------------|--------|-------|------------------------------|
| 0x00   | 0   | ap_start     | W1S    | 1     | Start kernel                 |
| 0x00   | 1   | ap_done      | R      | 1     | Kernel finished              |
| 0x10   | —   | status_clear | W1C    | 1     | Clear halted/error           |
| 0x18   | —   | halted       | R      | 1     | 1 = halted on error          |
| 0x20   | —   | error        | R      | 8     | Last error code              |
| 0x28   | —   | tx_id        | R      | 16    | TX id of halted txn          |
| 0x30   | —   | coeffs[4]    | RW     | 4×32  | Default coefficients         |
```

### C header

```python
def to_c_header(self, *, prefix: str) -> str
```

Generates `#define`s for offsets and bit widths, plus a packed struct for composite fields:

```c
/* Auto-generated from POLY_REGMAP — do not edit. */
#define POLY_AP_CTRL_OFFSET      0x00u
#define POLY_AP_START_BIT        0u
#define POLY_AP_DONE_BIT         1u
#define POLY_STATUS_CLEAR_OFFSET 0x10u
#define POLY_HALTED_OFFSET       0x18u
#define POLY_ERROR_OFFSET        0x20u
#define POLY_TX_ID_OFFSET        0x28u
#define POLY_COEFFS_OFFSET       0x30u
#define POLY_COEFFS_COUNT        4u
```

Such a generator would also be the natural place to diff the modelled layout against Vitis's `control.h` and fail loudly on drift.

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

## Quick reference {#vitis-quick-reference}

```python
from waveflow.hw.regmap import VitisRegMap, VitisRegMapMMIFSlave
```

| Operation | Code |
|---|---|
| Declare a Vitis regmap      | `VitisRegMap({"name": RegField(...), ...})` |
| Create Vitis slave          | `VitisRegMapMMIFSlave(sim=sim, bitwidth=32, regmap=regmap, on_start=self.on_start)` |
| Host launch a Vitis kernel  | `yield from regmap.start(master, base_addr=BASE)` |

The generic `RegMap` rows of this table are on
[Register Maps](../interface/primitive/regmap.md#quick-reference).

## See also

- [Register Maps](../interface/primitive/regmap.md) — the interface underneath: `RegField`,
  `RegAccess`, the offset table, and the AXI-Lite slave dispatch.
- [Host-activated kernel in HLS](./hostactivated.md) — what this lifecycle lowers to: one
  `ap_ctrl_hs` top-level function whose `s_axilite` block carries the same fields.

## Worked example

For an end-to-end walkthrough that puts these abstractions to work — declaring a `VitisRegMap`, running it in SimPy, generating the Vitis HLS kernel, and validating the measured RTL timing against the Python model — see the [Register Map example](../../examples/regmap/) in the Examples section.
