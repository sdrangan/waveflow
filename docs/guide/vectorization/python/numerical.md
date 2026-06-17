---
title: Numerical operations
parent: Python
grand_parent: Vectorization
nav_order: 1
audience: python
api: [DataArray, mult, add, sub, quantize]
summary: "The numerical model shared by every vectorized element type — defining a vector, and the two compute paths: the raw .val NumPy escape hatch, and the type-preserving operators (a*b + c then an explicit quantize). Includes when to use which."
---

# Numerical operations

Every vectorized value in Waveflow is a **`DataArray`** of some schema element type, backed by a NumPy
array — so a whole vector flows through one C-level NumPy call, not a Python loop over elements. This page
covers the numerical model shared by **all** element types (integer, float, fixed-point, complex): how to
define a vector, and the two ways to compute on one. The per-type specifics are on the
[integer](./integer.md) / [float](./float.md) / [fixed-point](./fixed.md) / [complex](./complex.md) pages.

## Defining vectors

A vector is `DataArray.specialize(element_type, max_shape=(...))` applied to array-like data; `.val` is the
underlying NumPy `ndarray`:

```python
import numpy as np
from waveflow.hw.dataschema import DataArray, FloatField

F32 = FloatField.specialize(32)
a = DataArray.specialize(F32, max_shape=(3,))(np.array([1.5, 2.5, -3.0], np.float32))
a.val                      # array([ 1.5,  2.5, -3. ], dtype=float32)  -- the raw ndarray
```

The **element type** sets how values are stored and how arithmetic grows — a `float` array is a native
float `ndarray`, while a [`FixedField`](../../schema/python/fixpoint.md) array is integer-backed *stored
codes*. Everything below works the same regardless of element type; only the derived result formats differ.

## Operations with `.val`

`DataArray.val` is the raw underlying `ndarray`. Reach through it and you get plain NumPy: maximum speed,
every NumPy function available, and **you** manage the result width and dtype.

```python
b = DataArray.specialize(F32, max_shape=(3,))(np.array([2.0, -1.5, 0.5], np.float32))
y = a.val * b.val + 0.25          # raw numpy float32 — you own the dtype
y                                  # array([ 3.25, -3.5 , -1.25], dtype=float32)
```

This is the right path for **float** math (IEEE floats don't grow, so raw NumPy *is* the bit-exact model)
or any time you genuinely want raw NumPy and will manage widths yourself.

## Type-preserving operators

The operators (`+`, `-`, `*`) on a `DataArray` are **type-preserving**: they read the operands' formats,
run the vectorized NumPy op underneath, and **derive the result format** with full precision — no silent
loss. They are sugar over the underlying `mult` / `add` / `sub` functions. Rounding back to a working
format is an **explicit** `quantize(x, fmt)` — exactly mirroring `ap_fixed<...> y = a*b + c;` in HLS, where
the product and sum grow to full width and the assignment is the one lossy step.

```python
from waveflow.hw.fixpoint import FixedField, from_real, quantize, to_real

Q = FixedField.specialize(8, 4)                  # ap_fixed<8, 4>
a = from_real([1.5, -2.0, 0.5], Q)
b = from_real([2.0,  1.5, -1.0], Q)
c = from_real([0.5,  0.25, -0.5], Q)

full = a * b + c                                  # ap_fixed<17, 9> — full precision, no loss
y    = quantize(full, Q)                          # ap_fixed<8, 4>  — the one explicit rounding
to_real(y)                                        # array([ 3.5 , -2.75, -1.  ])
```

### When to use which

| Path | Use it when | Cost you own |
|------|-------------|--------------|
| `.val` (numpy escape) | **float** math; or you genuinely want raw NumPy and will manage widths yourself | result dtype/width is on you |
| operators + `quantize` | **fixed-point** (and growth-aware **integer**) math you want bit-exact with HLS | nothing — formats are derived, loss is explicit |

The short rule:

- **Float** — `.val` is fine. IEEE-754 `float32`/`float64` don't grow, so raw NumPy *is* the bit-exact
  model; the operators are just NumPy passthrough over the same arrays.
- **Fixed-point** — use the **operators**. Fixed-point arithmetic grows bits (`a*b` is `Wa+Wb` wide) and
  rounds on assignment; the operators track that growth and keep the one rounding explicit, so your Python
  matches `ap_fixed` exactly. Doing fixed-point by hand through `.val` means re-deriving formats and
  rounding yourself — easy to get subtly wrong.
- **Integer** — either works; the operators add growth-aware width tracking (`a*b` → `Wa+Wb` bits,
  `a+b` → `+1`) with a fail-fast guard above 64 bits, which raw `.val` NumPy won't give you (it silently
  wraps). See [Integer vectorization](./integer.md).

Both paths keep data in NumPy arrays the whole way — that's what makes the simulation fast. The operators
just add the bit-growth bookkeeping on top.

## See also

- [`examples/basic_vec`](../../../examples/basic_vec/) — the worked MAC `y = a*b + c`, computed with these
  operators and checked **bit-exact against Vitis** for int / float / fixed.
- The per-type pages — [integer](./integer.md) / [float](./float.md) / [fixed-point](./fixed.md) /
  [complex](./complex.md) — for each element type's storage and result-format rules.
