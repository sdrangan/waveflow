---
title: Templating
parent: Module Code Generation
nav_order: 11
audience: hls
applies_to: [HwParam, HwConst]
api: [HwParamValue, kernel_signature, param_supports, validate_param_supports, HlsCodegenStep]
summary: "The C++ realization of component parameterization: HwParam values lower to concrete literal widths in the top kernel signature and to template parameters in .tpp hooks (HwParamValue carries the param name so the emitter chooses name vs literal); HwConst lowers to static constexpr (currently deferred); and param_supports emits one concrete <kernel>_<key> top per variant from a single class."
---

# Templating

This page is the **C++ realization** of component parameterization; the concept — what `HwParam` and
`HwConst` are, and when to use each — is [Hardware Components: Parameterization](../flows/parametrization.md).
Here we cover how each lowers into generated code.

## How `HwParam` lowers

A `HwParam` value reaches codegen as an [`HwParamValue`](../../../waveflow/hw/hw_module.py) — an
`int` that also carries its `.param_name`. That dual nature lets the generator emit either a **literal
value** or a **template-parameter name**, depending on where it lands:

- **Top kernel signature → literal width.** Top-level kernels are emitted *concrete* (no
  `template <int …>` block) so Vitis HLS can attach the AXI interfaces. The port widths in
  [`kernel_signature(comp)`](../../../waveflow/build/hwgen.py) come from
  [`_stream_template_arg`](../../../waveflow/build/hwgen.py), which **always emits the literal integer**
  from the endpoint's `bitwidth` (the variant's `HwParamValue`). So a `StreamIFMaster` declared with
  `out_bw = 64` becomes `hls::stream<streamutils::axi4s_word<64>>&` in the top.
- **`.tpp` hook → template parameter.** A hook that takes a *stream* argument is emitted as a
  **templated** function (`hook_signature(method, template_params=…)` names its stream args `WORD_BW`
  and so on), so its stub is written to a `.tpp` rather than a `.cpp` —
  [`HlsCodegenStep`](../../../waveflow/build/hwcodegen_steps.py) selects the extension per hook.
  Emitting it as `.tpp` keeps the template definition visible through the generated header's include
  path while the impl file stays [sticky](./codegen.md) across rebuilds. `poly`'s `evaluate` hook takes
  `s_in` / `m_out`, so it lands in `poly_evaluate_impl.tpp`; a hook with no stream argument (like
  `simp_fun`'s `compute`) is concrete and lands in a plain `.cpp`.

The decision between the two is `HwParamValue.param_name`: `SynthContext.cpp_param(name)` returns the
template-parameter name for a `HwParam` field, or `repr(value)` for a plain literal.

## How `HwConst` lowers

A [`HwConst`](../../../waveflow/hw/hw_module.py) is a class-level structural constant intended to
emit as a C++ `static constexpr T name = value;`. **This emission is currently deferred** (a follow-up
phase): `discover_hw_const(cls)` already surfaces the fields, but the generator does not yet write the
`static constexpr` lines. In the meantime a `HwConst` shapes generated structure indirectly through the
Python values it feeds (e.g. a static array extent that sizes a buffer).

## `param_supports` — emitting variant kernels

[`param_supports`](../../../waveflow/hw/hw_module.py) turns one component class into **multiple
concrete kernel tops**. Each variant key maps to a dict of `HwParam` overrides; codegen emits
`<cpp_kernel_name>_<key>` for each, alongside the default `<cpp_kernel_name>`.

```python
@dataclass
class VarKernel(FreeRunMod):
    cpp_kernel_name: ClassVar[str | None] = "var_kernel"
    param_supports: ClassVar[dict] = {"bw64": {"in_bw": 64}, "bw128": {"in_bw": 128}}

    in_bw: HwParam[int] = 32
    # ... endpoints declared with bitwidth=self.in_bw
```

emits `var_kernel`, `var_kernel_bw64`, and `var_kernel_bw128` — three concrete tops whose ports carry
literal widths of 32, 64 and 128, with no `template <...>` block on any of them.

> **No example in this repo uses `param_supports`**, so there is no worked reference to read — the
> snippet above is synthetic (though its output is verified). Where you *have* seen per-configuration
> kernels, as in `examples/vmac`'s `gen/ob8_q0_o0_m16/` tree, they come from a different mechanism: the
> build script generates a source set per configuration rather than one class declaring its variants.
> Reach for `param_supports` when one component should ship several fixed-width tops; reach for a build
> sweep when the configurations differ by more than `HwParam` values.

The mechanism: `_iter_variants(comp_class)` first validates with
[`validate_param_supports`](../../../waveflow/hw/hw_module.py), then yields the default variant
(suffix `""`) followed by one instance per `param_supports` entry — each built through the **normal
`__init__`** with the overrides applied (no immutability bypass). For each, `kernel_signature(comp,
variant_suffix=key)` names the top `<base>_<key>` and fills its ports with that instance's concrete
`HwParamValue` widths. This is the path to take when hardware integration needs **concrete top
signatures** rather than a single templated top.

## API

- [`HwParamValue`](../../../waveflow/hw/hw_module.py) — int + `.param_name`; drives template-name-vs-literal.
- [`kernel_signature(comp, variant_suffix="")`](../../../waveflow/build/hwgen.py) — the concrete top signature; appends `_<suffix>` for variants.
- [`param_supports`](../../../waveflow/hw/hw_module.py) / [`validate_param_supports`](../../../waveflow/hw/hw_module.py) — the variant map and its validation.
- [`HlsCodegenStep`](../../../waveflow/build/hwcodegen_steps.py) — selects `.cpp` vs `.tpp` per hook.

## Quick reference

- Top kernels are **concrete** — `HwParam` becomes a literal width in the signature.
- Hand-written hooks are **templated** — `HwParam` becomes a `.tpp` template parameter.
- `HwConst` → `static constexpr` is **deferred**; the value still shapes structure via Python.
- `param_supports` → one concrete `<kernel>_<key>` top per variant (default always emitted).
- The concept and the `HwParam` vs `HwConst` framing are in [Parameterization](../flows/parametrization.md).
