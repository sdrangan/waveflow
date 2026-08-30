---
title: Parameterization
parent: Hardware modules and Flows
nav_order: 4
audience: python
api: [HwParam, HwParamValue, HwConst, DynParam, discover_hw_const, discover_dyn_params, param_supports]
summary: "Parameterizing a HwModule (both flows). The family is one axis — when does the value bind — with four points: HwConst at class definition, HwParam at build (so distinct values mean distinct artifacts), DynParam at init/pre-sim, and a regmap register at runtime. The last two share one artifact across every value. param_supports declares a set of HwParam values to emit as kernel variants; its C++ realization is comp_codegen/templating."
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

Waveflow's parameter markers are all one axis — **when does the value bind?** — and where a knob sits
on it decides the thing you actually care about: whether changing it means building a new artifact.

| marker | binds at | one artifact per value? |
|---|---|---|
| [`HwConst[T]`](#hwconstt--class-level-structural-constants) | class definition | fixed structurally |
| [`HwParam[T]`](#hwparamt--per-instance-synthesis-parameters) | build / elaboration | **no** — distinct values are distinct artifacts (`mem_r_stream_32` vs `_64`) |
| [`DynParam[T]`](#dynparam) | init / pre-sim | **yes** |
| [regmap / `s_axilite`](../interface/primitive/regmap.md) | runtime, over AXI-Lite | **yes** — one bitstream serves every value |

`HwParam` is the one synthesizable code can take, and most of this page is about it. The bottom two
rows are the same idea at different times: a value set on a *built* thing rather than baked into it.

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

## `DynParam[T]` — init-time knobs on a fixed artifact {#dynparam}

[`DynParam[T]`](../../../waveflow/hw/hw_module.py) marks a field bound at **init / pre-sim** rather
than at build. The value is set on the instance and, for a generated model, emitted as a member
assignment — so **one artifact serves every value**, where a `HwParam` would have forced a second.

Its first and still-primary use is configuring [XSI testbench models](../comp_codegen/xsi_tb.md):

```python
class StreamDriver(...):
    in_bundle: DynParam[str] = ""       # which recorded bundle this driver plays
```

```cpp
s_cmd.in_bundle = "vectors/s_cmd";      // what the generated harness emits
```

[`discover_dyn_params(obj)`](../../../waveflow/hw/hw_module.py) returns `{field: value}` for every
`DynParam` whose value differs from the class default, and the generator emits one assignment per
entry. A field left at its default emits **nothing** — which is what lets a knob be added without
every existing harness growing a line.

The four declared today are all testbench-side configuration:

| field | on | says |
|---|---|---|
| `in_bundle` | `StreamDriver` | which bundle to play into the DUT |
| `out_bundle` | `StreamSink` | where to capture what comes out |
| `load_segs` | [`MemoryMod`](../memory/memorymod.md) | regions to load from bundles at `pre_sim` |
| `dump_segs` | `MemoryMod` | regions to dump to bundles at `post_sim` |

{: .note }
> **The axis is binding time, not synthesizable-vs-not.** It is tempting to read `DynParam` as "the
> marker for non-synthesized blocks" because every current user is a testbench model — but that is a
> fact about what has been built, not about the marker. Its synthesizable cousin is a
> [regmap / `s_axilite` register](../interface/primitive/regmap.md): also set on a finished artifact, just at
> runtime over a bus rather than at init in a C++ constructor.

{: .warning }
> **Bound once at `pre_sim`, and constant for the run.** A `DynParam` is not a per-cycle value and not
> a stream. Something that changes during a run is [state](../memory/hwstate.md), a stream payload, or
> a regmap write — and each of those is a different mechanism with a different cost.

## Choosing between them

|  | `HwParam[T]` | `HwConst[T]` | `DynParam[T]` |
|---|---|---|---|
| Scope | **per-instance** field | **class-level** attribute | **per-instance** field |
| Set when | at instantiation (`MyComp(in_bw=64)`) | at class definition | at init, before `pre_sim` |
| Varies | per instance, and per kernel **variant** | never — fixed for the class | per instance, on one built artifact |
| In simulation | int-like value (wrapped `HwParamValue`) | plain class attribute | a plain field |
| In codegen | template argument or literal | `static constexpr` | `<model>.<field> = <value>;` |
| Use for | configurable knobs: bus widths, datapath sizing | fixed structure: static array extents | config of a *generated model*: which vectors to play |

Rules of thumb:

- the value **sizes the hardware** → `HwParam`;
- it is a **fixed structural fact of the class** → `HwConst`;
- it **configures an already-built thing** → `DynParam`, or a
  [regmap register](../interface/primitive/regmap.md) if that thing is synthesized.

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

## See also

- [Module Code Generation: Templating](../comp_codegen/templating.md) — the C++ realization: how `HwParam` lowers into kernel signatures, `HwConst` into `static constexpr`, and `param_supports` into variant kernels.
- [Hardware modules](./modules.md) — where these fields are declared on the class.
- [Module structure](../comp_codegen/structure.md) — the generated kernel these parameters shape.
- [XSI testbenches](../comp_codegen/xsi_tb.md) — where `DynParam` assignments are emitted.
- [Register maps](../interface/primitive/regmap.md) — the runtime binding site, for synthesized blocks.

## Quick reference

- One axis: **when does the value bind?** — class definition (`HwConst`), build (`HwParam`), init
  (`DynParam`), runtime ([regmap](../interface/primitive/regmap.md)).
- `HwParam[T]` = per-instance synthesis knob; int-like in sim, wrapped `HwParamValue`, immutable after
  construction. **Distinct values mean distinct artifacts.** The only kind synthesizable code takes.
- `HwConst[T]` = class-level fixed structural constant; a plain class attribute in sim.
- `DynParam[T]` = init-time knob on a fixed artifact; emitted as `<model>.<field> = <value>;` via
  `discover_dyn_params`. Bound once at `pre_sim`, constant for the run.
- Sizes the hardware → `HwParam`; fixed fact of the class → `HwConst`; configures an already-built
  thing → `DynParam`, or a regmap register if it is synthesized.
- `param_supports` declares variant kernels (key → `HwParam` overrides); realization is [Templating](../comp_codegen/templating.md).
