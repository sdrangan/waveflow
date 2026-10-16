---
title: Host launch lifecycle
parent: Module Code Generation
nav_order: 3.5
audience: python
api: [VitisRegMap, VitisRegMapMMIFSlave, BoundRegMap, MMIFMaster, IrqIF, IrqIFSink, SimObj, Simulation]
summary: "How a host starts a HostActivated kernel and learns that it finished, modelled in SimPy — VitisRegMap's ap_ctrl_hs control block (the 0x00 word, the 0x10 user-field base), the ap_start / ap_done handshake VitisRegMapMMIFSlave runs around on_start, the kernel's interrupt line (gier / ier / isr, as Vitis generates them), and the BoundRegMap host surface (bind_master / run / wait_done). Includes what the model does not reproduce about real ap_ctrl_hs, and the planned host-artifact generators."
---

# Host launch lifecycle

A [`HostActivated`](../flows/modules.md) module does not run until a host starts it. That handshake
is `ap_ctrl_hs`: the host writes `ap_start`, the kernel runs, the kernel raises `ap_done` and, with
the interrupt enabled, its **interrupt line**; the host, asleep on that line, wakes and clears it. This page is the **Python model** of that lifecycle — what the simulation
does, so that the [generated kernel](./hostactivated.md) and the simulation agree about when work
begins and ends.

It sits on top of the register map, and only on top of it: the fields, the offsets and the AXI-Lite
dispatch underneath are [Register Maps](../interface/axi_mm/regmap.md). A register map is useful
without a launch; a launch is not possible without a register map.

## A minimal simulation

Two raw [`SimObj`](../sim/simobj.md)s exercising the launch-then-interrupt lifecycle over a
[`DirectMMIF`](../interface/axi_mm/modeling.md#directmmif): a `Kernel` holding a `VitisRegMapMMIFSlave` runs its `on_start`
when launched, and a `Host` holding an `MMIFMaster` and an `IrqIFSink` writes the inputs, asserts
`ap_start`, sleeps until the kernel's interrupt fires, and reads the result back. No `HwModule`. (`on_start` is the regmap-launched entry — see
the [SimObj lifecycle](../sim/simobj.md#its-lifecycle); the `yield from` mechanics are in
[Process generators](../sim/procgen.md).)

```python
from dataclasses import dataclass

from waveflow.hw.aximm import DirectMMIF, MMIFMaster
from waveflow.hw.clock import Clock
from waveflow.hw.dataschema import IntField
from waveflow.hw.irq import IrqIF, IrqIFSink
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
    """Holds the master; configures inputs, launches, waits on the interrupt, reads y back."""

    master: MMIFMaster | None = None
    kernel: Kernel | None = None
    clk: Clock | None = None

    def __post_init__(self) -> None:
        super().__post_init__()
        self.irq = IrqIFSink(name=f"{self.name}_irq", sim=self.sim)
        self.y: int | None = None

    def run_proc(self) -> ProcessGen[None]:
        rm = self.kernel.regmap.bind_master(self.master, base_addr=0)
        yield from rm.set("x", 5)
        yield from rm.set("a", 3)
        yield from rm.set("b", -4)
        yield from rm.run(self.irq)        # ier = gier = 1 (first time), ap_start, wait, clear isr
        self.y = yield from rm.get("y")
        print(f"done at t={self.sim.env.now}, y={self.y}")


sim = Simulation()
clk = Clock(freq=100e6)

kernel = Kernel(name="kernel", sim=sim, clk=clk)
host = Host(name="host", sim=sim, master=MMIFMaster(sim=sim, bitwidth=32), kernel=kernel, clk=clk)

link = DirectMMIF(sim=sim, clk=clk, byte_addressable=True)   # byte addresses (AXI-Lite convention)
link.bind("master", host.master)
link.bind("slave", kernel.s_lite)

irq = IrqIF(name="kernel_irq", sim=sim)                       # the kernel's interrupt wire
irq.bind("source", kernel.s_lite.interrupt())
irq.bind("sink", host.irq)

sim.run_sim()
```

`host.y` is `11` (`3*5 - 4`): the host's `set` writes land in the register fields, `start()` writes
`ap_start` which launches `on_start`, the slave sets `ap_done` and raises the interrupt when it
returns, and `run` wakes on it — reading nothing while it waits — before the host fetches `y`.
`bind_master` / `run` are the host-side
[`BoundRegMap`](#host-side-boundregmap) surface. See [SimObj](../sim/simobj.md) for the lifecycle.

## Host-side: BoundRegMap

Kernel-side `RegMap.get()` / `RegMap.set()` run in-process on the component object. On the host side, you usually have an `MMIFMaster` endpoint plus a base address, so reading and writing fields directly means repeating address arithmetic and schema wrapping at every call site.

`BoundRegMap` provides that host-side convenience surface by binding a `RegMap` instance to a master endpoint:

- `regmap.bind_master(master, base_addr=0) -> BoundRegMap`
- `BoundRegMap.get(name)` (coroutine): reads through `master.read_schema(...)` and returns native Python values (`int`, `IntEnum`, `float`, or schema instances for array/list fields).
- `BoundRegMap.set(name, value)` (coroutine): writes through `master.write_schema(...)`, auto-wrapping raw values using the field schema.
- `BoundRegMap.start()` (coroutine): convenience launch helper for `VitisRegMap` that writes `ap_start`.
- `BoundRegMap.enable_irq()` (coroutine): writes `ier = 1` (the `ap_done` interrupt only) then `gier = 1`. Once per kernel, **before** the launch it is to report — a kernel that finishes before its interrupt is enabled sets no `isr` bit and never wakes the host.
- `BoundRegMap.wait_done(irq)` (coroutine): sleeps on the host's `IrqIFSink` until the line is high, then writes `1` to `isr` (toggle-on-write), which clears it and lowers the line. No bus reads. Raises if `enable_irq` has not run.
- `BoundRegMap.run(irq)` (coroutine): the usual call — `enable_irq` the first time, then `start` + `wait_done`.
- `BoundRegMap.poll_end(field="ap_done", interval=…, max_polls=…)` (coroutine): reads a status field until it reads its completion value. A **debugging fallback** for a kernel whose interrupt is not wired: every look costs a bus read, and completion is seen up to one `interval` late.

Source class: [`BoundRegMap`](../../../waveflow/hw/regmap.py).

### Example (host-side testbench)

From [`examples/regmap/simp_fun.py`](../../../examples/regmap/simp_fun.py), `SimpFunHost.run_proc`:

```python
rm = self._regmap().bind_master(self.master, base_addr=self.base_addr)
yield from rm.set("x", self.case.x)
yield from rm.set("a", self.case.a)
yield from rm.set("b", self.case.b)
# Enable the ap_done interrupt, launch, and sleep until the kernel's interrupt
# line rises; then clear isr, which drops the line.  No ap_done reads.
yield from rm.run(self.irq)
self.y = yield from rm.get("y")
```

This keeps host-side register access aligned with kernel-side ergonomics while preserving typed schema conversions.

### Quick reference

- Use `bind_master(...)` once per `(master, base_addr)` pair.
- `get(name)` returns deserialized typed values.
- `set(name, value)` accepts either schema instances or raw values.
- `start()` / `enable_irq()` / `wait_done()` / `run()` (and the `poll_end()` fallback) are available on `VitisRegMap`-backed maps — for the `ap_start` launch and the completion wait.
- `BoundRegMap` is host-side only; kernel logic still uses `RegMap.get/set`.

---

## VitisRegMap

A `VitisRegMap` is a `RegMap` subclass that reproduces the s_axilite control layout Vitis HLS generates. The user only declares their own kernel-specific fields; the control block is added automatically, and the [`VitisRegMapMMIFSlave`](#vitisregmapmmifslave) manages the control bits (it clears `ap_done` on launch and sets it when the kernel returns).

The control block occupies the first 16 bytes, and **user fields start at `0x10`**:

| Offset | Contents |
|--------|----------|
| `0x00` | Control word — `ap_start` (bit 0), `ap_done` (bit 1), `ap_idle` (bit 2), `ap_ready` (bit 3) |
| `0x04` | `gier` — Global Interrupt Enable Register |
| `0x08` | `ier` — IP Interrupt Enable Register |
| `0x0C` | `isr` — IP Interrupt Status Register |
| `0x10` | user fields, placed by the rule below |

The four `ap_*` signals are **bits of one word**, not registers of their own.

Each user field goes, in declaration order, to the **lowest free address at or after `0x10`** that meets its alignment. The footprint depends on what the field is, counted in 4-byte slots:

| Field | Footprint | Alignment |
|-------|-----------|-----------|
| Input scalar (`RW`, `W`, …) | `ceil(W/32)` data slots + 1 control slot (`reserved`) | 4 bytes |
| Output scalar (`R`) | **twice** that: data, control (`ap_vld`), then an undocumented gap of equal size | 4 bytes |
| Array (`DataArray`, `cpp_storage="raw"`, 32-bit elements) | a memory region of `next_pow2(4·n)` bytes | its own size |

So `int x, a, b` then `int& y` land at `0x10`, `0x18`, `0x20`, `0x28` — the familiar 8-byte stride — but only because nothing follows the output. A field after an output skips the output's gap, a 64-bit scalar steps by 12 bytes rather than 16, and because placement is first fit, a small scalar can fill the hole an aligned array leaves *ahead* of fields declared before it. The rule is measured, not documented by AMD: `tests/hw/test_regmap_vitis_layout.py` pins it against 15 probe kernels and, under `-m vitis`, re-synthesizes them and diffs the model against the generated `ADDR_*` localparams.

An `RW` field the kernel *writes* is the one case the model gets wrong by construction: Vitis turns it into an in/out port with separate `<name>_i` and `<name>_o` registers, while `VitisRegMap` treats every `RW` field as an input. See [Bit-packed fields](../interface/axi_mm/regmap.md#bit-packed-fields) for the mechanism, and [Fidelity](#fidelity-what-is-and-is-not-modelled) for what the model does not reproduce.

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
SIMP_FUN_REGMAP = VitisRegMap({          # examples/regmap/simp_fun.py
    "x": RegField(Int32, RegAccess.RW, description="Input operand"),
    "a": RegField(Int32, RegAccess.RW, description="Multiply coefficient"),
    "b": RegField(Int32, RegAccess.RW, description="Bias term"),
    "y": RegField(Int32, RegAccess.R,  description="relu(a*x + b)"),
})
# offset_of("ap_start") == 0x00 with bit_offset_of("ap_start") == 0
# offset_of("ap_done")  == 0x00 with bit_offset_of("ap_done")  == 1
# offset_of("x") == 0x10, "a" == 0x18, "b" == 0x20 (inputs: 8 bytes each)
# offset_of("y") == 0x28 (an output: 16 bytes, but nothing follows it)
```

`VitisRegMap` requires `bitwidth=32` — the Vitis s_axilite control bus is 32 bits wide and the control block is defined on 4-byte words.

User-declared field names beginning with `ap_` are rejected at construction time to prevent collisions with current and future Vitis-reserved names, as are manual offsets inside the reserved `0x00`–`0x0f` control block.

### Fidelity: what is and is not modelled

The *layout* mirrors Vitis. The *side effects* are modelled only as far as the simulator needs:

- **`ap_done` / `ap_ready` are not clear-on-read.** Real hardware clears them when the host reads `0x00` (`COR`). The model clears them on the next `ap_start` instead, so a host can read `ap_done` repeatedly and keep seeing `1`.
- **`ap_start` is `W1S`, not `COH`.** It auto-clears once the launch hook has run rather than on the `ap_ready` handshake — the same net effect for a sim that launches synchronously.
- **`gier` / `ier` / `isr` drive the interrupt line, as Vitis generates them** (`*_control_s_axi.v`): `interrupt = gier[0] && isr != 0`; `isr[i]` is set at completion when `ier[i]` is, and **toggled** by a host write of 1. One simplification: `ap_ready` and `ap_done` coincide in the model (both at `on_start`'s return), so `isr` bits 0 and 1 are set together.
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
4. When `on_start` returns, the slave sets `ap_done` to `1` (in a `finally` block), marks itself idle, sets the `isr` bits enabled in `ier`, and re-evaluates the interrupt line. Subsequent `ap_start` writes launch a new invocation.

### The interrupt line

`slave.interrupt()` returns the kernel's interrupt as an [`IrqIFSource`](../../../waveflow/hw/irq.py) — the pysim twin of the `interrupt` port Vitis adds to an `s_axilite` control slave. Bind it to an `IrqIF` whose sink the host holds:

```python
irq = IrqIF(name="kernel_irq", sim=sim)
irq.bind("source", kernel.s_lite.interrupt())
irq.bind("sink", host.irq)                 # an IrqIFSink; the host calls rm.run(host.irq)
```

The line is **level**: high while `gier[0] && isr != 0`. A host that comes to wait after the kernel has already finished returns at once, so a fast kernel cannot be missed — provided its interrupt was enabled before the launch.

### What `on_start` should do

`on_start` is the kernel body. It is expected to be a generator that runs until either:

- It reaches an unrecoverable error condition, sets any user-defined status fields via `regmap.set(...)`, and `return`s. The slave will accept subsequent `ap_start` writes once it returns.
- It is written as a `while True:` loop over in-band commands that returns on an `END` command or on the first error -- one run is a batch of commands. This is the [streaming polynomial](../../examples/stream_inband/index.md)'s design: it clears its status at the start, and on an error sets `halted` / `error` / `tx_id`, closes any output burst it has started, and returns; the host resets the stream path before the next `ap_start`.

`on_start` must not be invoked from anywhere except the slave's launch path. Component authors do **not** write a `run_proc` for the kernel logic — there is no outer SimPy process waiting on a `start_event`. The slave is the sole entry point.

### What the slave does not do

- The slave **does** auto-manage `ap_done` / `ap_ready` / `ap_idle` (cleared on launch, set on return), but does **not** set any *user* status field. Error codes, transaction IDs, sticky flags, etc. are kernel-specific and remain the kernel author's responsibility (set via `regmap.set(name, value)` before `return`ing).
- The slave does **not** clear `ap_done` / `ap_ready` on read, and does **not** model `auto_restart` — see [Not yet modelled](#not-yet-modelled).

---

## Worked example: a one-shot kernel

The kernel from [`examples/regmap`](../../examples/regmap/index.md) computes `y = relu(a*x + b)`:
the host writes the arguments, starts the kernel, waits for `ap_done`, and reads the result.

### Field declarations and kernel side

The component declares its register map and an `on_start` method.  There is **no** `run_proc`,
no `start_event`, and no post-construction hook wiring -- the slave owns the launch lifecycle.

```python
@dataclass
class SimpFun(HostActivated):
    def __post_init__(self) -> None:
        super().__post_init__()
        # The Vitis control block (0x00-0x0f) is added automatically, so these
        # land at 0x10, 0x18, 0x20, 0x28.
        self.regmap = VitisRegMap({
            "x": RegField(Int32, RegAccess.RW, description="Input operand"),
            "a": RegField(Int32, RegAccess.RW, description="Multiply coefficient"),
            "b": RegField(Int32, RegAccess.RW, description="Bias term"),
            "y": RegField(Int32, RegAccess.R,  description="relu(a*x + b)"),
        })
        self.s_lite = VitisRegMapMMIFSlave(
            name=f"{self.name}_s_lite", sim=self.sim, bitwidth=32,
            regmap=self.regmap, on_start=self.on_start,
        )
        self.add_endpoint(self.s_lite)

    def on_start(self) -> ProcessGen[None]:
        """Invoked by VitisRegMapMMIFSlave when the host writes ap_start.  ap_done is
        cleared on the launch and set when this returns; the kernel writes only its result."""
        yield self.timeout(self.latency_cycles * self.clk.period)
        y = self.compute(self.regmap.get("x"), self.regmap.get("a"), self.regmap.get("b"))
        self.regmap.set("y", y)
```

### Host side

```python
rm = accel.regmap.bind_master(host.master, base_addr=BASE)
yield from rm.set("x", 5)
yield from rm.set("a", 3)
yield from rm.set("b", -4)
yield from rm.run(host.irq)          # enable the ap_done interrupt, start, sleep until done
y = yield from rm.get("y")           # 11
```

The same `VitisRegMap` object drives the SimPy simulation and would drive the (planned) HLS pragma generation and host driver class -- see below. Note that this is a single *declaration* of the fields, not a single source of the offsets: codegen emits no addresses, and Vitis assigns them from the `s_axilite` pragmas. `VitisRegMap` mirrors the layout Vitis documents; nothing yet checks the mirror -- see [Fidelity](#fidelity-what-is-and-is-not-modelled).

### A streaming kernel: status only

Here the register map carries the kernel's **arguments**, which is right for a one-shot kernel: one
call, one set of arguments, nothing else in flight.  A kernel that processes a **stream of commands**
is different.  If its configuration came over AXI-Lite while commands were queued on the stream, the
two paths would race.  The [streaming polynomial](../../examples/stream_inband/index.md) therefore
puts everything the kernel computes with on the stream, and its register map holds only status
(`halted`, `error`, `tx_id`, all `R`).  Its [contract](../../examples/stream_inband/index.md#the-contract)
and [Why this contract](../../examples/stream_inband/why_not.md) explain the split.

---

## Not yet modelled

- **`RegAccess.COR`** (clear-on-read): host reads return the current value, then the backing store is zeroed. Real `ap_done` / `ap_ready` are `COR`; the model clears them on the next `ap_start` instead.
- **`auto_restart` semantics in `VitisRegMapMMIFSlave`**: when bit 7 is set and `on_start` returns, the slave would immediately re-invoke `on_start` without another host write.
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
### SIMP_FUN register map

| Offset | Bit | Name         | Access | Width | Description                  |
|--------|-----|--------------|--------|-------|------------------------------|
| 0x00   | 0   | ap_start     | W1S    | 1     | Start kernel                 |
| 0x00   | 1   | ap_done      | R      | 1     | Kernel finished              |
| 0x10   | —   | x            | RW     | 32    | Input operand                |
| 0x18   | —   | a            | RW     | 32    | Multiply coefficient         |
| 0x20   | —   | b            | RW     | 32    | Bias term                    |
| 0x28   | —   | y            | R      | 32    | `relu(a*x + b)`              |
```

### C header

```python
def to_c_header(self, *, prefix: str) -> str
```

Generates `#define`s for offsets and bit widths, plus a packed struct for composite fields:

```c
/* Auto-generated from SIMP_FUN_REGMAP — do not edit. */
#define SIMP_FUN_AP_CTRL_OFFSET  0x00u
#define SIMP_FUN_AP_START_BIT    0u
#define SIMP_FUN_AP_DONE_BIT     1u
#define SIMP_FUN_X_OFFSET        0x10u
#define SIMP_FUN_A_OFFSET        0x18u
#define SIMP_FUN_B_OFFSET        0x20u
#define SIMP_FUN_Y_OFFSET        0x28u
```

Such a generator would also be the natural place to diff the modelled layout against Vitis's `control.h` and fail loudly on drift.

### Python driver class

```python
def to_python_driver(self, *, class_name: str) -> str
```

Generates a class that wraps an `MMIFMaster` with one accessor per field, returning deserialized Python values:

```python
class SimpFunDriver:
    def __init__(self, master: MMIFMaster, base_addr: int) -> None: ...

    def write_ap_start(self) -> ProcessGen[None]: ...

    def write_x(self, value: int) -> ProcessGen[None]: ...
    def write_a(self, value: int) -> ProcessGen[None]: ...
    def write_b(self, value: int) -> ProcessGen[None]: ...
    def read_y(self) -> ProcessGen[int]: ...
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
[Register Maps](../interface/axi_mm/regmap.md#quick-reference).

## See also

- [Register Maps](../interface/axi_mm/regmap.md) — the interface underneath: `RegField`,
  `RegAccess`, the offset table, and the AXI-Lite slave dispatch.
- [Host-activated kernel in HLS](./hostactivated.md) — what this lifecycle lowers to: one
  `ap_ctrl_hs` top-level function whose `s_axilite` block carries the same fields.

## Worked example

For an end-to-end walkthrough that puts these abstractions to work — declaring a `VitisRegMap`, running it in SimPy, generating the Vitis HLS kernel, and validating the measured RTL timing against the Python model — see the [Register Map example](../../examples/regmap/) in the Examples section.
