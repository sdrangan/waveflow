---
title: Integer vectorization
parent: Vectorization
nav_order: 2
---

# Integer vectorization

Integer arrays are `DataArray[IntField]` — numpy-backed, so the stored values are a
plain NumPy integer `ndarray` reachable through `.val`. The
[type-preserving operators](./index.md#the-two-paths) (`+`, `-`, `*`) compute over
the whole array in one NumPy call **and track bit growth** the way the HLS `ap_int`
datapath does, so the Python result width matches the hardware.

## Declaring and computing

`IntField.specialize(W, signed)` is the element type; `DataArray.specialize` wraps it
into a sized array:

```python
import numpy as np
from waveflow.hw.dataschema import DataArray, IntField

I8 = IntField.specialize(8, True)                       # ap_int<8>

def ia(vals):
    return DataArray.specialize(I8, max_shape=(len(vals),))(vals)

a, b, c = ia([3, -4, 5, 7]), ia([6, 7, -8, 2]), ia([1, -1, 2, -3])
y = a * b + c
np.asarray(y)                                            # array([ 19, -29, -38,  11])
```

This is the integer case of [`examples/basic_vec`](../../examples/basic_vec/) — one
elementwise MAC, no per-element Python loop.

## Growth-aware result widths

Each operator **derives the result width** so intermediates never overflow —
exactly the `ap_int` growth rules:

| op | result width | rule |
|----|--------------|------|
| `a * b` | `Wa + Wb` | product widths add |
| `a + b`, `a - b` | `max(Wa, Wb) + 1` | one carry bit |

Subtraction always returns a **signed** result (it can go negative). You can read
the derived type off the result:

```python
(a * b).element_type.get_bitwidth()     # 16   (8 + 8)
(a + b).element_type.get_bitwidth()     # 9    (max(8, 8) + 1)
y.element_type.get_bitwidth()           # 17   (the a*b is 16, +1 for the add)
y.element_type.__name__                 # 'Int17'
```

## The width-tracking caveat — `.val` doesn't grow

This is the heart of the two-paths distinction for integers. The operators grow the
type; the **raw NumPy escape** (`.val`) does not — NumPy keeps a fixed storage dtype
and **silently wraps** on overflow. For values that stay in range the two agree, but
the operator-tracked width is what protects you:

```python
a.val.dtype                              # dtype('int32')  -- raw numpy storage; arithmetic on
                                         # .val stays this dtype and wraps silently on overflow
(a * b).element_type.get_bitwidth()      # 16  -- the operator path grows the type instead
```

If a derived width would exceed **64 bits**, the operators **fail fast** rather than
let NumPy `int64` wrap invisibly:

```python
I40 = IntField.specialize(40, True)
big = DataArray.specialize(I40, max_shape=(1,))([1])
big * big                                # NotImplementedError: result width 80 exceeds the 64-bit limit
```

Wide (> 64-bit) support is future work; in practice integer datapaths stay well
under 64 bits. (For why a single 64-bit dtype is the deliberate choice, see the
[fixed-point vectorization](./fixed.md#single-64-bit-dtype-fail-fast-above-it)
page — the same guard backs both.)

## Mixed signed/unsigned is a v1 limitation

Mixing a signed and an unsigned integer array **raises**, because NumPy would coerce
`int64`/`uint64` to `float64` and silently lose exactness. Bring both operands to a
common signedness first:

```python
U8 = IntField.specialize(8, False)
u = DataArray.specialize(U8, max_shape=(1,))([1])      # ap_uint<8>
ia([1]) * u                              # NotImplementedError: mixed signed/unsigned ... not supported in v1
```

## See also

- [Vectorization overview](./index.md) — the two paths and when to use each.
- [Fixed-point vectorization](./fixed.md) — the same growth-then-`quantize` story
  with a binary point.
- [Fields](../schema/python/fields.md) — `IntField` and the scalar field types.
