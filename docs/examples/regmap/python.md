---
title: Python model
parent: Register mapped simple function
nav_order: 2
has_children: false
---

# Python model

We first describe how to build a python model for a hardware component with a Vitis Register map.

## Vitis Register Map



Waveflow's [`VitisRegMap`](../../guide/interface/regmap.md) class mirrors this layout: the user declares each register as a Python `RegField` with its data schema and access mode (`R`, `W`, `RW`, `W1C`, `W1S`), and Waveflow adds the same Vitis control block automatically. The one declaration drives the Python simulation, the generated HLS pragmas, and the host-side offset map.

Vitis HLS automatically generates this AXI-Lite slave whenever a kernel function has `#pragma HLS interface s_axilite` on its scalar arguments and on `return`. The Vitis-generated slave includes:

- A reserved control region at offsets `0x00–0x0f`: one control word at `0x00` packing `ap_start` (bit 0), `ap_done` (bit 1), `ap_idle` (bit 2) and `ap_ready` (bit 3), followed by the interrupt registers `gier` (`0x04`), `ier` (`0x08`) and `isr` (`0x0c`). The host writes `ap_start` to launch the kernel; the kernel raises `ap_done` when it returns.
- A user-defined region starting at `0x10`, with one register per scalar argument in declaration order — each on an 8-byte stride (a data word plus a control/reserved word).

Waveflow's [`VitisRegMap`](../../guide/interface/regmap.md) class mirrors this layout. The user declares only the application registers; the framework adds the control block, and the [`VitisRegMapMMIFSlave`](../../guide/interface/regmap.md) that wraps the regmap manages the control bits automatically.

## Describing the Register Map in Python

A register map is declared by passing a dict of `RegField` entries to `VitisRegMap`. Each `RegField` carries the field's data schema (here `Int32` — a specialised signed 32-bit `IntField`) and its host-side access mode (`R` / `W` / `RW` / `W1C` / `W1S`). Offsets are assigned automatically in declaration order; field names become the keys you use for `get` / `set` everywhere downstream.

```python
# examples/regmap/simp_fun.py
Int32 = IntField.specialize(bitwidth=32, signed=True)

self.regmap = VitisRegMap({
    "x": RegField(Int32, RegAccess.RW, description="Input operand"),
    "a": RegField(Int32, RegAccess.RW, description="Multiply coefficient"),
    "b": RegField(Int32, RegAccess.RW, description="Bias term"),
    "y": RegField(Int32, RegAccess.R,  description="relu(a*x + b)"),
})
```

After construction, the layout is `ap_start@0x00` bit 0, `ap_done@0x00` bit 1, then `x@0x10`, `a@0x18`, `b@0x20`, `y@0x28` — mirroring the layout Vitis HLS allocates from the equivalent `s_axilite` pragmas, which it records in `<proj>/solution1/.autopilot/db/coregen/control.h`.

Note what does and does not follow from that. The generated C++ contains no offsets at all — Vitis derives them from the `s_axilite` pragmas — so these Python offsets are used only *inside* the simulation, where the host proxy and the slave read the same table. That makes the model self-consistent, and it makes the addresses you see here the ones a real driver would use. It does not make them *enforced*: nothing yet diffs `VitisRegMap` against `control.h`, so the mirror could drift silently if Vitis changed its layout.

The full set of `RegAccess` modes and the access matrix they imply is documented in [Register Maps](../../guide/interface/regmap.md).

## Creating the Kernel with the Register Map

The kernel is a `HostActivated` component (the [host-activated](../../guide/flows/modules.md) kind — it carries a regmap and the host launches it). It owns the regmap and binds it to a `VitisRegMapMMIFSlave` endpoint. The slave receives a host-side reference to the component's `on_start` method — the SimPy generator the framework invokes when the host writes `ap_start`.

```python
@dataclass
class SimpFun(HostActivated):
    cpp_kernel_name: ClassVar[str | None] = "simp_fun"
    cpp_namespace:   ClassVar[str | None] = "simp_fun_impl"
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        self.regmap = VitisRegMap({ ...as above... })
        self.s_lite = VitisRegMapMMIFSlave(
            name=f"{self.name}_s_lite", sim=self.sim, bitwidth=32,
            regmap=self.regmap, on_start=self.on_start,
        )
        self.add_endpoint(self.s_lite)
```

The behavior of the kernel itself lives in `on_start`. It reads the input registers, calls the compute method, and writes the result back to `y`. **It does not touch `ap_done`** — `VitisRegMapMMIFSlave` clears `ap_done` to 0 when `ap_start` fires and sets it to 1 in a `finally` block when `on_start` returns, exactly the way real Vitis HLS manages the bit.

```python
def on_start(self) -> ProcessGen[None]:
    y = self.compute(
        self.regmap.get("x"),
        self.regmap.get("a"),
        self.regmap.get("b"),
    )
    self.regmap.set("y", y)

@synthesizable
def compute(self, x: Int32, a: Int32, b: Int32) -> Int32:
    return Int32(relu_affine(int(x.val), int(a.val), int(b.val)))
```

These two methods have **opposite** fates in codegen, and it is worth being precise about which is which:

- **`on_start` becomes the kernel body.** It is read from source and translated statement by statement — the register reads, the call, the register write. It is *not* sim-only.
- **`compute` is not translated at all.** `@synthesizable` marks a **hook boundary**: codegen emits a *declaration* and a call to it, then a stub for you to fill in. Its C++ lives in the hand-written [`simp_fun_compute_impl.cpp`](../../../examples/regmap/simp_fun_compute_impl.cpp); the Python body above stays as the simulation golden and is never lowered.

What Vitis supplies is not `on_start`'s body but the **launch** around it: the `s_axilite` adapter that decodes a write to `ap_start` and raises `ap_done` on return — `on_start` is that adapter's callback, which is why it exists exactly when a regmap does. See [Component structure](../../guide/comp_codegen/structure.md) and [Automatic vs. manual](../../guide/comp_codegen/automatic.md).

## Creating the Host

The host is the SimPy stand-in for the CPU driver. It is a plain `SimObj` with an `MMIFMaster` connected to the kernel's slave via a `DirectMMIF` (the in-process AXI-Lite link). Inside `run_proc`, it obtains a bound regmap proxy with `regmap.bind_master(...)` and then talks to the kernel by name rather than by address.

```python
@dataclass(kw_only=True)
class SimpFunHost(SimObj):
    case: SimpFunCase
    clk:  Clock
    latency_cycles:       int = 4
    poll_interval_cycles: int = 4
    max_polls:            int = 32

    def run_proc(self) -> ProcessGen[None]:
        rm = self._regmap().bind_master(self.master, base_addr=self.base_addr)

        yield from rm.set("x", self.case.x)
        yield from rm.set("a", self.case.a)
        yield from rm.set("b", self.case.b)
        yield from rm.start()                                       # write 1 to ap_start

        yield self.timeout(self.latency_cycles * self.clk.period)   # don't poll too early

        self.ap_done = yield from rm.poll_end(                       # poll ap_done until == 1
            interval=self.poll_interval_cycles * self.clk.period,
            max_polls=self.max_polls,
        )
        self.y = yield from rm.get("y")
```

Three things are doing real work here:

1. **`bind_master`** wraps the regmap with a host-side proxy so subsequent `get` / `set` / `start` / `poll_end` calls all dispatch through the AXI-Lite master at the configured `base_addr`. The proxy mirrors the kernel-side `regmap.get/set` API — same names, different yield discipline because bus traffic is asynchronous.
2. **`rm.start()`** is the convenience wrapper for "write 1 to `ap_start`."
3. **`rm.poll_end(interval=..., max_polls=...)`** polls the auto-emitted `ap_done` field every `interval` seconds until it reads 1 (the default target), then returns the read value. Raises `RuntimeError` after `max_polls` if the kernel never completes.

The `latency_cycles` initial wait is an optimization: the host knows the kernel cannot possibly be done before that many cycles, so the early reads would just be wasted bus traffic. `poll_interval_cycles` then controls how aggressively the host hits the bus while waiting. Polling every clock cycle would saturate the AXI-Lite link in a real system — `poll_end` makes the cadence an explicit, tunable parameter.

> **In production**, the host wouldn't poll at all — it would wait on an AXI-Lite interrupt line. The polling path here is a pedagogical and debugging convenience. Waveflow's interrupt-based wait API will land in a future release.

## Using the interface

The kernel and host are wired together by a `DirectMMIF` link inside a small `connect()` helper, then the simulation is driven by `simulate_case()`:

```python
def connect(sim, host, accel, clk):
    lite_link = DirectMMIF(sim=sim, clk=clk, byte_addressable=True)
    lite_link.bind("master", host.master)
    lite_link.bind("slave",  accel.s_lite)
    host._regmap_ref = accel.regmap

def simulate_case(case: SimpFunCase, *, clk_freq=100e6, latency_cycles=4):
    sim = Simulation()
    clk = Clock(freq=clk_freq)
    accel = SimpFun(name="simp_fun", sim=sim, clk=clk, latency_cycles=latency_cycles)
    host  = SimpFunHost(name="host", sim=sim, case=case, clk=clk, latency_cycles=latency_cycles)
    connect(sim, host, accel, clk)
    sim.run_sim()
    return SimpFunSimResult(case=case, y=int(host.y),
                            ap_done=int(host.ap_done), passed=bool(host.passed))
```

After `sim.run_sim()` returns, `host.y` holds the computed result and `host.ap_done` holds the completion bit; the `SimpFunSimResult` packages them with the input case for downstream comparison against the C-sim and RTL-cosim runs (covered on the [C and RTL simulation](./rtlsim.md) page).

A direct invocation looks like:

```python
result = simulate_case(SimpFunCase(x=5, a=3, b=-4))
assert result.y == 11 and result.ap_done == 1 and result.passed
```

That's the full Python model — one regmap declaration, one kernel hook, one host coroutine, and a wiring helper. Everything else is reusable framework.

## Next

- [System simulation](./pysim.md) — running the kernel and host together in SimPy, and confirming the model works before any testbench exists.
- [Sequential execution](./seqtb.md) — the `SeqTB` that invokes the kernel directly, and produces the golden and the cycle timing the synthesis pages validate against.
