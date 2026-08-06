---
title: The Python model
parent: Basic vector arithmetic
nav_order: 1
---
# The Python golden model

The golden is one vectorized MAC per numeric kind — `a*b + c` over `DataArray`s, with **no
per-element loop**. The same expression, three inner types. (For the *concepts* — operators,
growth rules, the `.val` escape — see the [vectorization guide](../../guide/vectorization/);
here we just build this example.)

## Integer — growth-aware operators

```python
from waveflow.hw.dataschema import DataArray, IntField

I8 = IntField.specialize(8, True)                         # ap_int<8>
ia = lambda vals: DataArray.specialize(I8, max_shape=(len(vals),))(vals)

a, b, c = ia([3, -4, 5, 7]), ia([6, 7, -8, 2]), ia([1, -1, 2, -3])
y = a * b + c                                             # -> Int17
y.element_type.get_bitwidth()                             # 17  (a*b is 16, +c adds a carry bit)
```

The operators **derive the result width** (`a*b` → `8+8=16`, `+c` → `+1`), so `Int17`
exactly mirrors `ap_int<17> y = a*b + c;` — no overflow, no manual sizing.

## Float — numpy passthrough

```python
import numpy as np
from waveflow.hw.dataschema import FloatField

F32 = FloatField.specialize(32)
fa = lambda vals: DataArray.specialize(F32, max_shape=(len(vals),))(np.array(vals, dtype=np.float32))

a, b, c = fa([1.5, 2.5, -3.0]), fa([2.0, -1.5, 0.5]), fa([0.25, 1.0, -0.5])
y = a * b + c                                             # float32, no growth
```

For float, the operators are numpy passthrough — `a*b + c` is the same **two roundings**
numpy does (multiply, then add). The Vitis kernel must reproduce *exactly* that (no fused
FMA) — see [the float kernel](./vitis.md#float).

## Fixed — grow then quantize

```python
from waveflow.hw.fixpoint import FixedField, from_real, quantize

Q = FixedField.specialize(8, 4)                          # ap_fixed<8,4>
a = from_real([1.5, -2.0, 0.5], Q)
b = from_real([2.0, 1.5, -1.0], Q)
c = from_real([0.5, 0.25, -0.5], Q)
y = quantize(a * b + c, Q)                               # full precision, then ONE quantize
```

`a*b + c` grows to full precision (the product widens, the sum grows a bit); the explicit
`quantize(..., Q)` rounds back to `ap_fixed<8,4>` — exactly what `ap_fixed<8,4> y = a*b + c;`
does on assignment.

## The golden bits

Each result is serialized to the **stored bits** the kernel will emit — integer/fixed via
`to_bits`, float via the IEEE bit-view:

```python
import numpy as np
from waveflow.utils.fixputils import to_bits

# integer / fixed: stored W-bit integers
int_bits = [int(x) for x in to_bits(np.asarray(y), 8)]
# float: raw IEEE bits
float_bits = [int(u) for u in np.asarray(y).astype(np.float32).view(np.uint32)]
```

These golden bit-vectors go into `expected.json`; the kernel's output is compared against
them in [the eval step](./eval.md).
