---
title: Writing it in Python
parent: Sequential (host-activated)
nav_order: 0
audience: python
api: [HostActivated, VitisRegMap, RegField, VitisRegMapMMIFSlave, synthesizable, sim_only]
summary: "How to describe a host-activated module in Python: subclass HostActivated, declare the application registers in a VitisRegMap, wire a VitisRegMapMMIFSlave whose on_start is the kernel body, and mark the datapath @synthesizable. The scalar simp_fun (y = relu(a*x + b)) end to end, plus the two markers that decide what reaches hardware."
---

# Writing it in Python

A host-activated module is a **function in hardware**: the host writes the inputs, pulses a start
bit, and waits. In Python that is a [`HostActivated`](./modules.md) whose `on_start` runs once per
launch.

The worked example here is `simp_fun` — `y = relu(a·x + b)`, three scalars in and one out — because
it is the smallest thing that still has every part of the shape.
([`examples/regmap/simp_fun.py`](../../../examples/regmap/simp_fun.py).)

## The shape

```python
@dataclass
class SimpFun(HostActivated):
    cpp_kernel_name: ClassVar[str | None] = "simp_fun"
    cpp_namespace:   ClassVar[str | None] = "simp_fun_impl"

    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))
    latency_cycles: int = 4

    def __post_init__(self) -> None:
        super().__post_init__()
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
        yield self.timeout(self.latency_cycles * self.clk.period)
        y = self.compute(self.regmap.get("x"), self.regmap.get("a"), self.regmap.get("b"))
        self.regmap.set("y", y)

    @synthesizable
    def compute(self, x: Int32, a: Int32, b: Int32) -> Int32:
        return Int32(relu_affine(int(x.val), int(a.val), int(b.val)))
```

Four things are being declared, and it is worth separating them.

**The register map is the control plane.** You declare only the *application* registers. `VitisRegMap`
adds the Vitis control block itself — `ap_start` and `ap_done` share the control word at `0x00`, and
`0x04`/`0x08`/`0x0c` are the interrupt registers — so `x`, `a`, `b`, `y` land at `0x10` onward at
Vitis's 8-byte scalar stride. `RegAccess` says who may write: `RW` is host-written input, `R` is
kernel-written output.

**`on_start` is the kernel body.** It runs once per launch, and returning is what makes the kernel
report done — `ap_done` is managed by `VitisRegMapMMIFSlave`, cleared on `ap_start` and set on
return. You never write it.

**`@synthesizable` marks the hook boundary.** `compute` is where the datapath lives. Its Python body
is the *simulation model*; codegen emits its declaration and a `// TODO` stub, and the C++ you write
there is yours. Nothing checks the two against each other — that is what the flow's
[C-simulation gate](./sequential.md#the-three-gates) is for.

**`self.timeout(...)` models latency and emits nothing.** It is `@sim_only`, so the extractor strips
it; the generated C++ is byte-identical with or without it. This is how a module carries a cycle
prediction without that prediction leaking into hardware.

## What may go in `on_start`

`on_start` is *extracted*, not run, so it is written in the synthesizable subset: a fixed set of
statement shapes, equality-only conditions, and a fixed vocabulary of endpoint operations
(`self.regmap.get/set`, stream `get`/`write`, calls to `@synthesizable` hooks). Anything else is
rejected loudly rather than compiled into doubtful C++. The full list is
[Extractor](../comp_codegen/extractor.md).

The rule that catches people first: **you may not read mutable `self.X`**. `self.latency_cycles` is
readable only because it is consumed inside a `@sim_only` call. A value the kernel genuinely needs is
either a register (declare it in the regmap), a build-time constant
([`HwParam`](./parametrization.md)), or persistent storage
([`HwState`](../memory/hwstate.md), declared with `add_state`).

## Simulating it

The module is a `SimObj`, so it runs in SimPy with no toolchain: give it a host that writes the
registers, pulses start, and reads the result back. That run is the **golden** — the same Python that
generates the C++ produces the expected values, and the C-simulation gate compares against it
bit-exactly.

## Next

- [Flow steps](./sequential_flowsteps.md) — the build recipe from here to a verified measurement.
- [How it is realized in HLS](../comp_codegen/structure.md) — the generated top, `ap_ctrl_hs`, and
  the `s_axilite` register ports.
- [Register Map example](../../examples/regmap/) — this module, one page per step, against real code.
