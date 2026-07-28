---
title: Parameterization
parent: Hardware modules and Flows
nav_order: 4
audience: python
api: [HwParam, HwParamValue, HwConst, discover_hw_const, param_supports]
summary: "Parameterizing a HwModule (both flows): HwParam[T] for per-instance, configurable synthesis knobs (bus widths, datapath sizing — vary per instance and per generated kernel variant) and HwConst[T] for class-level, fixed structural constants (static array extents). The crux is per-instance-configurable vs class-level-fixed. param_supports declares a set of HwParam values to emit as kernel variants; its C++ realization is comp_codegen/templating."
---

# Parameterization

Both flows build on a `HwModule`, and either kind of component can be parameterized — the synthesis
surface lives on `HwModule` itself, so this page applies to a `HostActivated` kernel and a
`FreeRunMod` alike.

## Why parameterize

A [`HwModule`](./modules.md) is a *template* for hardware, not a single fixed block. Parameterizing
it lets **one class describe a family of hardware**: the same datapath sized for a 32-bit bus or a
64-bit bus, an accelerator built for 8-bit or 16-bit operands, a coefficient bank with 4 or 8 taps.
That buys three things:

- **One source, many concrete kernels.** A single class emits several Vitis tops at build time, one
  per configuration — see `param_supports` below.
- **Sizing the datapath.** Bus widths and lane counts ripple through the generated port signatures and
  the lane geometry, so the parameter *is* the hardware size.
- **Reusable IP.** A component parameterized on its widths/shapes is reusable across designs without
  editing its body.

Waveflow has two parameter markers, and choosing between them is the crux: **`HwParam` is
per-instance and configurable; `HwConst` is class-level and fixed.**

## `HwParam[T]` — per-instance synthesis parameters

[`HwParam[T]`](../../../waveflow/hw/hw_module.py) marks a dataclass field as a synthesis parameter
bound **at instantiation** — `comp = MyComp(in_bw=64)` — and potentially varied **per generated kernel
variant**. From [`examples/stream_inband/poly.py`](../../../examples/stream_inband/poly.py):

```python
in_bw:    HwParam[int] = 32
out_bw:   HwParam[int] = 32
aximm_bw: HwParam[int] = 32
```

(VMAC parameterizes its operand / accumulator / output widths the same way.) Three properties matter:

- **Int-like in simulation.** A `HwParam` field behaves as a normal Python integer — arithmetic,
  comparison, indexing all work — so the SimPy model runs with the concrete value.
- **Identity-preserving for codegen.** `HwModule.__post_init__` wraps each `HwParam` value as an
  [`HwParamValue`](../../../waveflow/hw/hw_module.py) — an `int` subclass that remembers the
  `.param_name` it came from. That lets the emitter decide between a C++ *template parameter name* and
  a literal value (the [realization](../comp_codegen/templating.md) page).
- **Immutable after construction.** `HwModule.__setattr__` raises if you reassign a `HwParam` field
  once `__post_init__` has finished — the value is frozen for the instance's hardware identity.

## `HwConst[T]` — class-level structural constants

[`HwConst[T]`](../../../waveflow/hw/hw_module.py) marks a **class attribute** that is fixed for the
class — the same for every instance — typically a structural extent like a static array size. From its
docstring:

```python
class CoeffArray(DataArray):
    ncoeff: HwConst[int] = 4
    max_shape = (ncoeff,)
```

In Python simulation a `HwConst` is just a regular class attribute (the framework does not enforce
immutability — the marker signals intent). [`discover_hw_const(cls)`](../../../waveflow/hw/hw_module.py)
walks the MRO and returns every `HwConst` field so codegen can find them.

## HwParam vs. HwConst — the distinction

|  | `HwParam[T]` | `HwConst[T]` |
|---|---|---|
| Scope | **per-instance** field | **class-level** attribute |
| Set when | at instantiation (`MyComp(in_bw=64)`) | at class definition |
| Varies | per instance, and per kernel **variant** | never — fixed for the class |
| In simulation | int-like value (wrapped `HwParamValue`) | plain class attribute |
| Use for | configurable knobs: bus widths, datapath sizing | fixed structure: static array extents |

Rule of thumb: if a value should be **dialable per instance / per generated variant**, it is a
`HwParam`; if it is a **fixed structural fact of the class**, it is a `HwConst`.

## `param_supports` — declaring kernel variants

To emit more than one concrete kernel from a class, declare
[`param_supports`](../../../waveflow/hw/hw_module.py): a map of *variant key → `HwParam` overrides*.

```python
class MyKernel(HwModule):
    cpp_kernel_name = "my_kernel"
    in_bw: HwParam[int] = 32
    param_supports = {
        "bw64":  {"in_bw": 64},
        "bw128": {"in_bw": 128},
    }
```

This **declares** that the build should emit `my_kernel` (defaults), `my_kernel_bw64`, and
`my_kernel_bw128`. [`validate_param_supports`](../../../waveflow/hw/hw_module.py) checks the keys
(valid C identifiers) and that every override names a real `HwParam` field. *How* those variants are
generated — concrete top functions per key — is the realization page:
[Module Code Generation: Templating](../comp_codegen/templating.md).

> Forward pointer: a third parameter binding-site, `XSIParam`, is planned for the concurrent flow's
> testbench participants (a value that becomes a constructor argument of a generated XSI model, and
> makes the class non-synthesizable). It is not built yet.

## See also

- [Module Code Generation: Templating](../comp_codegen/templating.md) — the C++ realization: how `HwParam` lowers into kernel signatures, `HwConst` into `static constexpr`, and `param_supports` into variant kernels.
- [Hardware modules](./modules.md) — where these fields are declared on the class.
- [Module structure](../comp_codegen/structure.md) — the generated kernel these parameters shape.

## Quick reference

- `HwParam[T]` = per-instance synthesis knob; int-like in sim, wrapped `HwParamValue`, immutable after construction.
- `HwConst[T]` = class-level fixed structural constant; a plain class attribute in sim.
- Per-instance-configurable → `HwParam`; class-level-fixed → `HwConst`.
- `param_supports` declares variant kernels (key → `HwParam` overrides); realization is [Templating](../comp_codegen/templating.md).
