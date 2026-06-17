---
title: Templating
parent: Component Code Generation
nav_order: 5
audience: hls
applies_to: [HwParam]
api: [HwParam, HwParamValue]
summary: "How HwParam values become template-aware C++: HwParamValue preserves parameter identity for codegen, and parameterized hook stubs are emitted as .tpp so template definitions stay visible via the header include path."
---

# Templating

## Concept

Templating is driven by `HwParam` annotations on component fields. During construction, `HwComponent.__post_init__` wraps these values as `HwParamValue`, which preserves parameter identity for codegen while behaving like normal integers at runtime.

Hook stubs become `.tpp` when parameterized template arguments are needed. This keeps template definitions visible via the generated header include path while preserving sticky impl files across rebuilds.

## API

- [`HwParam[T]`](../../../waveflow/hw/hw_component.py) marks template-driven fields.
- [`HwParamValue`](../../../waveflow/hw/hw_component.py) stores the bound param name.
- [`HwComponent.__post_init__`](../../../waveflow/hw/hw_component.py) auto-wraps parameter values.
- [`HlsCodegenStep`](../../../waveflow/build/hwcodegen_steps.py) selects `.cpp` vs `.tpp` per hook.

## Example

From [`examples/stream_inband/poly.py`](../../../examples/stream_inband/poly.py), bus widths are declared with `HwParam` so codegen can specialize stream types:

```python
in_bw:  HwParam[int] = 32
out_bw: HwParam[int] = 32
aximm_bw: HwParam[int] = 32
```

These values flow into generated stream/regmap signatures and hook template decisions.

## Quick reference

- Declare template-like component knobs as `HwParam[...]`.
- `HwParam` fields are immutable after construction.
- Hook template requirements trigger `_impl.tpp` generation.
- Header output includes templated impl files for visibility.
- Use plain fields for runtime values that are not template parameters.
