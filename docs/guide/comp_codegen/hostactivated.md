---
title: Host-activated kernel in HLS
parent: Module Code Generation
nav_order: 3
audience: hls
applies_to: [HostActivated]
api: [kernel_files_to_str, kernel_signature, VitisRegMapMMIFSlave, synthesizable]
summary: "A HostActivated module lowers to a control_driven_kernel: one ap_ctrl_hs top-level function whose s_axilite register block carries the application registers plus the Vitis control word. Walks simp_fun (y = relu(a*x + b)) from Python to generated C++ — the regmap becoming ports, the pragma that makes the kernel host-startable, on_start becoming the body, and the hook left as a hand-written stub."
---

# Host-activated kernel in HLS

A [`HostActivated`](../flows/sequential.md) module lowers to a **`control_driven_kernel`**: a single
`ap_ctrl_hs` top-level function that a host launches and waits on. Its registers become `s_axilite`
ports, its `on_start` becomes the function body, and each `@synthesizable` hook becomes a call to C++
you write by hand.

That is easier to see than to describe, so this page walks one module all the way through.

## The example

`simp_fun` — `y = relu(a·x + b)`, three scalars in and one out. It is the same module
[Writing it in Python](../flows/sequential_python.md) builds, so the two pages are the two halves of
one story. ([`examples/regmap/simp_fun.py`](../../../examples/regmap/simp_fun.py).)

```python
@dataclass
class SimpFun(HostActivated):
    cpp_kernel_name: ClassVar[str | None] = "simp_fun"
    cpp_namespace:   ClassVar[str | None] = "simp_fun_impl"

    def __post_init__(self) -> None:
        super().__post_init__()
        self.regmap = VitisRegMap({
            "x": RegField(Int32, RegAccess.RW, description="Input operand"),
            "a": RegField(Int32, RegAccess.RW, description="Multiply coefficient"),
            "b": RegField(Int32, RegAccess.RW, description="Bias term"),
            "y": RegField(Int32, RegAccess.R,  description="relu(a*x + b)"),
        })
        self.s_lite = VitisRegMapMMIFSlave(..., regmap=self.regmap, on_start=self.on_start)
        self.add_endpoint(self.s_lite)

    def on_start(self) -> ProcessGen[None]:
        y = self.compute(self.regmap.get("x"), self.regmap.get("a"), self.regmap.get("b"))
        self.regmap.set("y", y)

    @synthesizable
    def compute(self, x: Int32, a: Int32, b: Int32) -> Int32:
        return Int32(relu_affine(int(x.val), int(a.val), int(b.val)))
```

And here is the whole of what that generates:

```cpp
#include "simp_fun.hpp"

void simp_fun(
    ap_int<32>& x,
    ap_int<32>& a,
    ap_int<32>& b,
    ap_int<32>& y
) {
#pragma HLS INTERFACE s_axilite port=x            bundle=control
#pragma HLS INTERFACE s_axilite port=a            bundle=control
#pragma HLS INTERFACE s_axilite port=b            bundle=control
#pragma HLS INTERFACE s_axilite port=y            bundle=control
#pragma HLS INTERFACE s_axilite port=return       bundle=control
    ap_int<32> _y_local = simp_fun_impl::compute(x, a, b);
    y = _y_local;
}
```

Four things happened. Taking them in turn.

## 1. Each register became a port

The four `RegField`s became the function's four arguments, as references, with their C++ types
resolved from the field schemas (`Int32` → `ap_int<32>`). `RegAccess` did not change the signature —
`x`, `a`, `b` and `y` are all `&` — because direction is carried by the register block, not by the
argument.

The `bundle=control` on each pragma is what collects them into **one** AXI-Lite slave rather than four
separate interfaces. That bundle is the register block the host writes.

You declared four registers and got four ports, but the block has more in it than that. `VitisRegMap`
adds the Vitis control registers itself: `ap_start` and `ap_done` share the control word at `0x00`,
and `0x04`/`0x08`/`0x0c` are the global and IP interrupt registers. Your application registers land at
`0x10` onward, at Vitis's 8-byte scalar stride. That is why the Python declares only `x`, `a`, `b`,
`y` — the control plane is not yours to write.

## 2. `port=return` made it host-startable

```cpp
#pragma HLS INTERFACE s_axilite port=return bundle=control
```

This is the line that matters most, and it is the one with no counterpart in the Python.

The `return` port carries the kernel's **block-level control protocol**. `HostActivated` means
`ap_ctrl_hs` — "handshake": the block gets `ap_start` ("go"), `ap_done` ("finished"), plus `ap_idle`
and `ap_ready`. A caller raises `ap_start`, the kernel runs to completion, raises `ap_done`, and
stops.

Putting `return` in the `control` bundle puts that handshake **inside the AXI-Lite block**, so a host
starts the kernel by writing bit 0 of `0x00` rather than by driving a pin.

Two consequences follow from `ap_ctrl_hs`, and everything about this flow follows from them:

- **The kernel is a function** — arguments in, one run, a return. A testbench can *call* it, and Vitis
  will build the RTL co-simulation harness around that call for you. The
  [free-running kernel](./freerunning.md) gives both of those up.
- **`ap_done` is not yours to raise.** `VitisRegMapMMIFSlave` manages it: cleared on `ap_start`, set
  when `on_start` returns. Returning *is* signalling done.

## 3. `on_start` became the body

`on_start` is [extracted](./extractor.md) — read as source, never run, and translated statement by
statement. Two statements in, two statements out:

| Python | C++ |
|---|---|
| `self.regmap.get("x")` | `x` — the port is already in scope, so the read is the identifier |
| `y = self.compute(...)` | `ap_int<32> _y_local = simp_fun_impl::compute(x, a, b);` |
| `self.regmap.set("y", y)` | `y = _y_local;` |

`regmap.get` and `set` do not become function calls; they become a use and an assignment of the port
that the register already is. The local is named `_y_local` rather than `y` precisely because `y` is
taken — by the output port it is about to be written to.

A [`HwState`](../memory/hwstate.md) declaration, if the module had one, would appear as a `static` at
the top of this same function body.

## 4. `compute` became a call to code you write

This is the seam. `on_start` was lowered; `compute` was **not**. What codegen emitted for it is a
declaration and a stub:

```cpp
#include "simp_fun.hpp"

namespace simp_fun_impl {
ap_int<32> compute(ap_int<32> x, ap_int<32> a, ap_int<32> b) {
    // TODO: implement compute
    return ap_int<32>(0);
}
}
```

You fill that in; the checked-in
[`simp_fun_compute_impl.cpp`](../../../examples/regmap/simp_fun_compute_impl.cpp) is the real one. The
Python body of `compute` stays behind as the **simulation golden** — it is never lowered, and nothing
checks the two against each other. That is what the flow's
[C-simulation gate](../flows/sequential.md#the-three-gates) is for: it compares the C++ kernel's
outputs against what the Python model produced, bit for bit.

The namespace comes from `cpp_namespace` (default `<kernel>_impl`), which is why the call site reads
`simp_fun_impl::compute`.

## The file set

`kernel_files_to_str` emits `simp_fun.hpp` (the declaration plus schema includes), `simp_fun.cpp`
(above), and `simp_fun_compute_impl.cpp` (the stub) — one stub per hook. The first two are
framework-owned and rewritten on every build; the stub is **sticky** and never overwritten once you
have edited it. That distinction, and `.cpp`-vs-`.tpp` routing for templated hooks, is
[Generated files](./codegen.md).

## A module with no register map

Not every host-activated kernel has a regmap. One without it is still `ap_ctrl_hs` — its arguments
simply arrive as ordinary ports rather than registers, and its control is on raw pins instead of in an
AXI-Lite block. `block_scale` is one.

This is easy to mistake for free-running, because both extract a method other than `on_start`. The
difference is the protocol, and raw pins are still a handshake.

## See also

- [Sequential (host-activated)](../flows/sequential.md) — the flow, its three gates, and the ladder
  from Python simulation to RTL co-simulation.
- [Writing it in Python](../flows/sequential_python.md) — this same module, from the other side.
- [Host launch lifecycle](./host_launch.md) — the Python side of `ap_ctrl_hs`: `VitisRegMap`, the
  `ap_start` / `ap_done` handshake, and the `BoundRegMap` host surface this kernel is driven from.
- [Endpoint interfaces](./interface.md) — the full endpoint → port table (streams and `m_axi` too).
- [Module structure](./structure.md) — the contract, and which method is extracted for which kind.
