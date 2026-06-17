---
title: HwConst
parent: Hardware Components
nav_order: 2
audience: python
applies_to: [HwComponent, DataArray]
api: [HwConst, discover_hw_const]
summary: "HwConst[T] — class-level compile-time structural constants (e.g. static array extents); contrast with the per-instance HwParam."
---

# HwConst

## Concept

`HwConst[T]` marks class-level constants intended to represent compile-time values attached to schema/component definitions. It communicates intent to readers and codegen paths that treat the value as fixed for the class.

> The codegen side — emitting a `HwConst` as a C++ `static constexpr` — is deferred and belongs to
> the forthcoming **component codegen** section; in Python simulation a `HwConst` is a regular class
> attribute.

Use `HwConst` for structural constants that do not vary across instances (for example static array extents). Use `HwParam` when a value should vary per instance and potentially per generated kernel variant.

## API

- [`HwConst`](../../../waveflow/hw/hw_component.py)
- [`discover_hw_const(cls)`](../../../waveflow/hw/hw_component.py)

## Example

Minimal pattern used by array-like schemas:

```python
class CoeffArray(DataArray):
    ncoeff: HwConst[int] = 4
    max_shape = (ncoeff,)
```

## Quick reference

- `HwConst` is class-level, not per-instance.
- Prefer `HwConst` for fixed structural constants.
- Prefer `HwParam` for configurable synthesis knobs.
- Current C++ `static constexpr` emission is deferred.
- In Python simulation, constants are still regular class attributes.
