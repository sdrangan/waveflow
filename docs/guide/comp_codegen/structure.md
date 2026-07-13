---
title: Component structure
parent: Component Code Generation
nav_order: 1
audience: hls
applies_to: [HwComponent]
api: [kernel_files_to_str, cpp_kernel_name, extract_kernel, synthesizable]
summary: "The Vitis HLS realization of an HwComponent: each component becomes one top-level kernel function whose arguments are its endpoints; the execution model is a free-running loop (run_proc) or a one-time regmap-launched call (on_start), selected by whether the component carries a VitisRegMapMMIFSlave; the kernel body comes from the @synthesizable methods (auto-extracted or, with impl_file=, hand-written)."
---

# Component structure

## Concept

Each [`HwComponent`](../components/) is generated into **one Vitis HLS top-level function** — the
kernel. The function name defaults to the class name in `snake_case` with a trailing `_component`
stripped (`PolyAccelComponent → poly_accel`), overridable per class with
`cpp_kernel_name: ClassVar[str] = "..."`. Its arguments correspond one-to-one to the component's
declared **endpoints**; how each endpoint type maps to a port (`hls::stream` / `m_axi` /
`s_axilite`) is the next page, [Endpoint interfaces](./interface.md).

For the simplest case, a regmap-only component generates a kernel whose arguments are just its
register fields. From [`examples/regmap/gen/simp_fun.hpp`](../../../examples/regmap/gen/simp_fun.hpp):

```cpp
void simp_fun(
    ap_int<32>& x,
    ap_int<32>& a,
    ap_int<32>& b,
    ap_int<32>& y
);

namespace simp_fun_impl {
    ap_int<32> compute(ap_int<32> x, ap_int<32> a, ap_int<32> b);
}
```

The top function `simp_fun` is the kernel; `simp_fun_impl::compute` is a hook it calls.

## The execution model: free-running vs. regmap-launched

The definition-side view — how you *choose* the mode when writing the component — is
[Defining a component: Execution models](../components/overview.md#execution-models); this section is its
C++ realization. A generated kernel runs in one of two modes, and the generator picks the mode — and the
Python method it extracts as the kernel body — from the component's endpoints:

| Mode | Kernel entry | Selected when | Control |
|---|---|---|---|
| **Regmap-launched** (one-time call) | `on_start` | the component has a `VitisRegMapMMIFSlave` endpoint | host writes `ap_start`; kernel runs once, sets `ap_done` |
| **Free-running** (endless loop) | `run_proc` | otherwise | `ap_ctrl_hs` / stream-driven |

The selection lives in [`extract_kernel(comp)`](../../../waveflow/build/hwcodegen.py): it scans the
component's endpoints and, if any is a `VitisRegMapMMIFSlave`, extracts `on_start`; otherwise it
extracts `run_proc`. (For `HwTestbench` subclasses it routes to `main()` instead — see
[Testbench](./testbench.md).)

> **Declaring the mode by class.** Rather than have the mode inferred, a component can *declare* it: a
> [`FreeRunComp`](../components/taxonomy.md) sets `control_mode = FREE_RUNNING` and implements
> **`run_iter`** — *one firing*, the `hls::task` body the runtime re-fires (the `while True` is the
> base's). Codegen then never has to recognize a `while` loop at the extracted root. The full
> class-per-execution-model hierarchy (`SynthComp` / `FreeRunComp` / host-activated / `HwTestbench`) is
> [Component taxonomy](../components/taxonomy.md).

The regmap-launched body is *invocation-style*: it reads inputs from the register map, computes, and
writes the result, returning once. From
[`examples/regmap/simp_fun.py`](../../../examples/regmap/simp_fun.py):

```python
def on_start(self) -> ProcessGen[None]:
    y = self.compute(
        self.regmap.get("x"),
        self.regmap.get("a"),
        self.regmap.get("b"),
    )
    self.regmap.set("y", y)
```

`VitisRegMap` auto-manages `ap_done` (cleared on `ap_start`, set when `on_start` returns), so the
body only writes its result. A free-running component instead implements `run_proc` as a long-lived
process that loops over its stream/`m_axi` ports.

> **Sim-only lifecycle hooks are not synthesized.** `pre_sim` and `post_sim` exist for the Python
> [simulation lifecycle](../sim/simobj.md#its-lifecycle) only — the generator never lowers them. Only the
> selected kernel entry (`on_start` / `run_proc`, or `main` for a testbench) and the `@synthesizable`
> methods it calls become C++.

## Where the kernel body comes from

The top function's body and its helper functions come from the component's
[`@synthesizable`](../../../waveflow/hw/synth.py) methods. There are two kinds:

- **Auto-extracted** — a plain `@synthesizable` method is lowered from the synthesizable subset of
  its Python by the [extractor](./extractor.md) (`simp_fun_impl::compute`, above, is one).
- **Hand-written** — `@synthesizable(impl_file="…tpp")` is a stub: the generator emits a *call* to a
  C++ function you write in that `.tpp`, and the method's Python body remains the simulation golden.
  This is the [Custom Hooks](../custom_hooks/) path.

Either way the hook is emitted into a namespace derived from the kernel name (or
`cpp_namespace`), so the call site is `<kernel>_impl::<hook>(...)` as in the `simp_fun_impl::compute`
declaration above.

## API

- [`kernel_files_to_str(comp_class, output_dir=".", impl_dir=None)`](../../../waveflow/build/hwgen.py) — generate the kernel file contents for a component class.
- [`cpp_kernel_name(comp_class)`](../../../waveflow/build/hwgen.py) — the default top-function name (`CamelCase → snake_case`, drop `_component`).
- [`extract_kernel(comp)`](../../../waveflow/build/hwcodegen.py) — selects `on_start` (regmap-launched) vs. `run_proc` (free-running) and resolves the kernel `HwStmt`.
- [`@synthesizable`](../../../waveflow/hw/synth.py) — marks the methods that become the kernel body / hooks; with `impl_file=` it becomes a hand-written stub.

## Quick reference

- One `HwComponent` → one top-level kernel function; its args are its endpoints.
- A `VitisRegMapMMIFSlave` endpoint ⇒ regmap-launched (`on_start`, `ap_start`/`ap_done`); otherwise free-running (`run_proc`).
- `pre_sim` / `post_sim` are simulation-only and are never synthesized.
- Kernel body = the `@synthesizable` methods; `impl_file=` defers a body to a hand-written [Custom Hook](../custom_hooks/).
- Override the kernel name with `cpp_kernel_name`; the hook namespace with `cpp_namespace`.
