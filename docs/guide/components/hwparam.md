---
title: HwParam
parent: Hardware Components
nav_order: 1
audience: python
applies_to: [HwComponent]
api: [HwParam, HwParamValue]
summary: "HwParam[T] — per-instance synthesis-parameter fields: int-like in simulation, identity-preserving for codegen (wrapped as HwParamValue), immutable after construction."
---

# HwParam

## Concept

`HwParam[T]` marks component fields that should be treated as synthesis parameters. These fields behave like Python integers in simulation but preserve parameter identity for code generation.

During `HwComponent.__post_init__`, raw values for `HwParam` fields are wrapped as `HwParamValue`. This wrapper preserves which parameter name produced the value so emitters can substitute template-aware expressions where needed.

> The codegen side — how a `HwParam` lowers to a C++ template parameter / kernel-signature
> argument — belongs to the forthcoming **component codegen** section; it is interim-documented
> under [Synthesis templating](../synthesis/templating.md).

## API

- [`HwParam`](../../../waveflow/hw/hw_component.py)
- [`HwParamValue`](../../../waveflow/hw/hw_component.py)
- [`HwComponent.__post_init__`](../../../waveflow/hw/hw_component.py)
- [`HwComponent.__setattr__`](../../../waveflow/hw/hw_component.py)

## Example

From [`examples/stream_inband/poly.py`](../../../examples/stream_inband/poly.py):

```python
in_bw: HwParam[int] = 32
out_bw: HwParam[int] = 32
aximm_bw: HwParam[int] = 32
```

These parameters drive generated kernel signatures and stream/regmap bitwidths.

## Quick reference

- Declare synthesis parameters as `HwParam[...]`.
- Values are auto-wrapped to `HwParamValue` during construction.
- `HwParam` fields are immutable after construction.
- Use plain fields for mutable runtime state.
- See [Synthesis templating](../synthesis/templating.md) for codegen flow.
