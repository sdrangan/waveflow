---
title: Param supports
parent: Component Code Generation
nav_order: 4
audience: hls
applies_to: [HwParam]
api: [param_supports]
summary: "How one component class emits multiple concrete kernel entry points (<kernel>_<key>) from a param_supports map of HwParam overrides — for when Vitis needs concrete tops rather than templated top-level functions."
---

# Param supports

## Concept

`param_supports` lets a component emit multiple concrete kernel entry points from one class definition. Each variant key maps to a dictionary of `HwParam` overrides, and codegen emits `<kernel>_<key>` for each variant alongside the default `<kernel>` function.

This pattern is used when Vitis needs concrete tops rather than templated top-level functions. Parameterized internals stay reusable while exported kernel symbols remain explicit.

## API

- [`param_supports`](../../../waveflow/hw/hw_component.py) class variable on `HwComponent`.
- [`validate_param_supports(comp_class)`](../../../waveflow/hw/hw_component.py) checks keys and overrides.
- Kernel naming follows `cpp_kernel_name` + `_<variant_key>` in [`kernel_signature`](../../../waveflow/build/hwgen.py).

## Example

Minimal pattern using documented APIs:

```python
class MyKernel(HwComponent):
    cpp_kernel_name = "my_kernel"
    in_bw: HwParam[int] = 32
    param_supports = {
        "bw64": {"in_bw": 64},
        "bw128": {"in_bw": 128},
    }
```

This emits `my_kernel`, `my_kernel_bw64`, and `my_kernel_bw128` tops with concrete widths.

## Quick reference

- Keys must be valid C identifiers.
- Override keys must reference declared `HwParam` fields.
- The default kernel is always emitted.
- Duplicate resolved variants raise warnings.
- Use variant tops when hardware integration needs fixed signatures.
