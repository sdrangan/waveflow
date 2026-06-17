---
title: Float vectorization
parent: Python
grand_parent: Vectorization
nav_order: 3
audience: python
applies_to: [FloatField]
api: [FloatField]
summary: "The float vectorized model — FloatField arrays as native NumPy float arrays, and when float fits versus fixed-point or integer."
---

# Float vectorization

Floating-point arrays are `DataArray[FloatField]` — numpy-backed by an IEEE-754
`float32` or `float64` `ndarray`. Float is the simplest vectorization case: IEEE
floats **don't grow**, so the [type-preserving operators](./numerical.md#type-preserving-operators)
are just **NumPy passthrough** over the same arrays, and the
[`.val` escape hatch](./numerical.md#operations-with-val) is equally correct.

## Operators are passthrough

`FloatField.specialize(W)` picks the width (32 or 64); the operators run the NumPy
op and keep the same float type — no width derivation, no `quantize` step:

```python
import numpy as np
from waveflow.hw.dataschema import DataArray, FloatField

F32 = FloatField.specialize(32)                          # float (single precision)

def fa(vals):
    return DataArray.specialize(F32, max_shape=(len(vals),))(np.array(vals, np.float32))

a, b, c = fa([1.5, 2.5, -3.0]), fa([2.0, -1.5, 0.5]), fa([0.25, 1.0, -0.5])
y = a * b + c
np.asarray(y)                                            # array([ 3.25, -2.75, -2.  ], dtype=float32)
y.element_type.get_bitwidth()                            # 32  (no growth — float32 in, float32 out)
```

This is the float case of [`examples/basic_vec`](../../../examples/basic_vec/). A `float64` array
stays 64-bit the same way.

## When to use `.val` vs the operators

For float, **`.val` is fine** — it's the recommended path when you want raw NumPy.
Because floats don't grow and don't need an explicit `quantize`, raw NumPy on `.val`
*is* the bit-exact model:

```python
y = a.val * b.val + c.val                                # identical result, raw numpy
y.dtype                                                  # dtype('float32')
```

The operators give you nothing extra here beyond keeping the value in a `DataArray`
(the bit-growth bookkeeping they add for [integer](./integer.md) and
[fixed-point](./fixed.md) has no float analog). Use whichever reads better;
[fixed-point is the case that *needs* the operators](./numerical.md#when-to-use-which).

## Golden references — matching the kernel bit-for-bit

The reason float still belongs in a bit-exact story is the **two-roundings**
subtlety. `y = a*b + c` in IEEE float rounds **twice** — once for the product, once
for the add. A fused multiply-add (FMA) rounds **once** and gives a *different* last
bit. NumPy's `a*b + c` is two roundings; to match it, the generated Vitis kernel is
compiled with `-ffp-contract=off` so the C++ `a*b + c` is also two roundings, never
a fused FMA (see `examples/basic_vec/run.tcl`).

To compare against the kernel you look at the **raw IEEE bits**, not the printed
decimal — reinterpret the `float32` array as `uint32`:

```python
[int(u) for u in np.asarray(a).astype(np.float32).view(np.uint32)]
# [1069547520, 1075838976, 3225419776]
```

`examples/basic_vec` emits exactly these bit-vectors as the Python golden and
asserts the Vitis C-sim output bits match them exactly.

## See also

- [Vectorization overview](../index.md) — the two paths and when to use each.
- [Integer vectorization](./integer.md) / [Fixed-point vectorization](./fixed.md) —
  the cases where the operators' growth tracking matters.
- [Fields](../../schema/python/fields.md) — `FloatField` and the scalar field types.
