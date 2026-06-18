---
title: Defining a component
parent: Hardware Components
nav_order: 1
audience: python
applies_to: [HwComponent]
api: [HwComponent, Component, add_endpoint, VitisRegMapMMIFSlave, synthesizable]
summary: "How you define a HwComponent: subclass HwComponent, declare its typed interface endpoints in __post_init__ and register each with add_endpoint, and mark compute methods @synthesizable — walked through the regmap simp_fun example. The same class is the source for the generated C++ kernel."
---

# Defining a component

## The class

A hardware module is a subclass of [`HwComponent`](../../../waveflow/hw/hw_component.py), which
extends [`Component`](../../../waveflow/hw/component.py) (the base simulation object with named
endpoints and the SimPy lifecycle). Components are written as dataclasses; runtime configuration and
simulation-only state are plain fields, while synthesis knobs use [`HwParam` and
`HwConst`](./parameterization.md).

A few class-level (`ClassVar`) settings steer the generated C++ but are inert in simulation:
`cpp_kernel_name` (override the kernel function name), `cpp_namespace` (the hook namespace), and
`param_supports` (emit variant kernels) — all covered in [Component Code Generation](../comp_codegen/).

## Declaring endpoints

A component's ports are **interface endpoints**, declared in `__post_init__` and registered with
[`add_endpoint(...)`](../../../waveflow/hw/component.py) (which records the endpoint under its name
and back-links it to the component). From
[`examples/regmap/simp_fun.py`](../../../examples/regmap/simp_fun.py):

```python
@dataclass
class SimpFunComponent(HwComponent):
    cpp_kernel_name: ClassVar[str | None] = "simp_fun"
    cpp_namespace: ClassVar[str | None] = "simp_fun_impl"
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))

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
```

`simp_fun` declares one endpoint — an AXI-Lite register map
([`VitisRegMapMMIFSlave`](../../../waveflow/hw/regmap.py)). A component with a datapath declares more:
[`examples/stream_inband/poly.py`](../../../examples/stream_inband/poly.py)'s `PolyAccelComponent`
declares a stream input, a stream output, and a regmap, and registers all three:

```python
def __post_init__(self) -> None:
    super().__post_init__()
    self.s_in  = StreamIFSlave( name=f'{self.name}_s_in',  sim=self.sim, bitwidth=self.in_bw)
    self.m_out = StreamIFMaster(name=f'{self.name}_m_out', sim=self.sim, bitwidth=self.out_bw)
    self.s_lite = VitisRegMapMMIFSlave(name=f'{self.name}_s_lite', sim=self.sim, ...)
    for ep in (self.s_in, self.m_out, self.s_lite):
        self.add_endpoint(ep)
```

Which endpoint *types* exist, and the master/slave role of each, is [Endpoint methods](./endpoints.md);
the endpoint classes and their construction live in [Interfaces](../interface/).

## Behavior and compute

A component's behavior is the **methods on its endpoints**, driven from a [lifecycle](../sim/simobj.md#its-lifecycle)
method — `on_start` for a regmap-launched component (like `simp_fun`) or `run_proc` for a free-running
one. Compute that must synthesize to hardware is marked
[`@synthesizable`](../../../waveflow/hw/synth.py):

```python
def on_start(self) -> ProcessGen[None]:
    y = self.compute(self.regmap.get("x"), self.regmap.get("a"), self.regmap.get("b"))
    self.regmap.set("y", y)

@synthesizable
def compute(self, x: Int32, a: Int32, b: Int32) -> Int32:
    return Int32(relu_affine(int(x.val), int(a.val), int(b.val)))
```

## Execution models

How you structure that behavior puts the component in one of two modes — and the mode follows from its
endpoints:

- **Free-running (`run_proc`).** The default: a long-lived [lifecycle](../sim/simobj.md#its-lifecycle) process that loops
  over the component's stream / `m_axi` ports, the way a streaming datapath runs continuously. You
  implement `run_proc`.
- **Regmap-launched (`on_start`).** *Invocation-style.* A component that declares a
  [`VitisRegMapMMIFSlave`](./endpoints.md) is launched by the host (which writes `ap_start`); its
  `on_start` reads its inputs from the register fields, computes, writes the results, and returns once.
  `simp_fun` above is this mode; `poly` uses it as a control path while streaming sample data through
  `s_in` / `m_out`.

The selection is automatic — you don't set it explicitly: a component carrying a `VitisRegMapMMIFSlave` is
regmap-launched (you implement `on_start`); otherwise it is free-running (you implement `run_proc`). The C++
realization of each — the `ap_start` / `ap_done` handshake vs. `ap_ctrl_hs`, and how the kernel entry is
chosen — is [Component Code Generation: Component structure](../comp_codegen/structure.md).

## The same class generates C++

This one class is also the source for the synthesizable kernel: the generator turns the component
into a top-level Vitis HLS function whose **arguments are these same endpoints**, and lowers the
`@synthesizable` methods into the kernel body. That dual view — same `simp_fun` component, as a
generated `void simp_fun(ap_int<32>& x, …)` — is
[Component Code Generation: Component structure](../comp_codegen/structure.md).

## Quick reference

- Subclass `HwComponent` (a dataclass); call `super().__post_init__()` first.
- Declare each port in `__post_init__` and register it with `add_endpoint(...)`.
- Keep synthesis knobs in [`HwParam` / `HwConst`](./parameterization.md); everything else is a plain field.
- Drive behavior from `on_start` (regmap-launched) or `run_proc` (free-running) — see [Lifecycle](../sim/simobj.md#its-lifecycle).
- Mark hardware compute `@synthesizable`; the [endpoint methods](./endpoints.md) move the data.
