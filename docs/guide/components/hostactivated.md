---
title: Host-activated components
parent: Hardware Components
nav_order: 3
audience: python
applies_to: [HostActivated]
api: [HostActivated, VitisRegMapMMIFSlave, VitisRegMap, RegField, run_once]
summary: "HostActivated — the synthesizable HwComponent the host launches one run at a time over an AXI-Lite register map. It carries a VitisRegMapMMIFSlave and you implement on_start; each ap_start trigger reads the input registers, computes, writes the outputs, and returns (Vitis ap_ctrl_hs). Walked through the simp_fun kernel, plus run_once — the one-call invocation that mirrors the generated C++ kernel call."
---

# Host-activated components

## What it is

A [`HostActivated`](../../../waveflow/hw/hw_hostactivated.py) is a synthesizable `HwComponent` that the
host launches **one run at a time**. It carries a register map and you implement **`on_start`**: each
launch reads its inputs from the registers, computes, writes the outputs, and returns. This is the
Waveflow model for a **simple Vitis-activated kernel** — an accelerator the CPU *configures and
triggers*, as opposed to a [free-running datapath](./freerun.md) that runs on its own.

## The Vitis activation protocol

A host-activated kernel exposes an **AXI-Lite** (`s_axilite`) control interface — a
[`VitisRegMapMMIFSlave`](../interface/regmap.md) carrying a register map. Waveflow's `VitisRegMap`
automatically prepends the two Vitis control registers, `ap_start` and `ap_done`; you declare only the
application registers. One invocation is the **`ap_ctrl_hs`** handshake:

1. The host writes the **input** registers over AXI-Lite (for `simp_fun` below: `x`, `a`, `b`).
2. The host sets **`ap_start`**.
3. The kernel runs **once** — `on_start` reads the inputs, computes, and writes the **output** registers
   (`y`) — then signals **`ap_done`** (auto-managed: cleared on `ap_start`, set when `on_start` returns).
4. The host polls `ap_done`, then reads the output registers.

Because it runs once per trigger, `HostActivated` sets `control_mode = PER_INVOCATION` and rejects a
`run_iter` (that is a [free-running](./freerun.md) kernel's entry).

## A simple example — `simp_fun`

[`examples/regmap/simp_fun.py`](../../../examples/regmap/simp_fun.py) computes `y = relu(a·x + b)`:

```python
@dataclass
class SimpFunComponent(HostActivated):
    cpp_kernel_name: ClassVar[str | None] = "simp_fun"

    def __post_init__(self) -> None:
        super().__post_init__()
        # VitisRegMap adds the Vitis control block (ap_start / ap_done are bits
        # of the 0x00 word); you declare only the app registers, from 0x10 up.
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
        y = self.compute(self.regmap.get("x"), self.regmap.get("a"), self.regmap.get("b"))
        self.regmap.set("y", y)

    @synthesizable
    def compute(self, x: Int32, a: Int32, b: Int32) -> Int32:
        return Int32(relu_affine(int(x.val), int(a.val), int(b.val)))
```

The three parts map to the protocol above: the **register map** declares the inputs (`RW`) and the
output (`R`); **`on_start`** is the kernel body run on each trigger; and the `@synthesizable`
**`compute`** is the hardware math that lowers to C++.

## Invoking it once — `run_once`

A host-activated kernel's C++ realization *is* a function — `simp_fun(x, a, b, y)` — so its natural
invocation is a single call. [`run_once`](../../../waveflow/hw/hw_hostactivated.py) gives the Python side
the same shape:

```python
y = dut.run_once(x, a, b)      # or the shorthand:  y = dut(x, a, b)
```

Its signature is **derived from the register map**, so it cannot drift from the kernel: the inputs are
the host-writable (`RW`/`W`) fields in declaration order, and the return is the host-readable (`R`)
field(s). Internally it does the protocol above — set the input registers, run `on_start`, read the
output — in one call.

`run_once` is the **closest representation of a single sequential invocation** of the kernel. A
sequential testbench written with it lowers **1:1** to the generated C++ call `simp_fun(x, a, b, y)` —
which is exactly how Vitis C-simulation and co-simulation drive the kernel. (Today `run_once` covers
pure register-map kernels like `simp_fun`; stream-bearing kernels are a follow-on.)

*How* a host-activated kernel is generated and verified end-to-end is the
[realization flows](../flows/) section.
