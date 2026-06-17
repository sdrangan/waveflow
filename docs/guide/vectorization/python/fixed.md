---
title: Fixed-point vectorization
parent: Python
grand_parent: Vectorization
nav_order: 3
audience: python
applies_to: [FixedField]
api: [FixedField]
summary: "The fixed-point vectorized model — FixedField arrays, the two paths (.val NumPy escape hatch vs type-preserving operators), and the ap_fixed result-format growth."
---

# Fixed-Point Vectorization

This is the [vectorization](../index.md) story for fixed-point — the **compute**.
The fixed-point *type* itself (the `ap_fixed` model, `QMode`/`OMode`, the
defaults-match-Vitis contract) lives on the
[FixedField type page](../../schema/python/fixpoint.md); read that first if you haven't.

Fixed-point arrays use [`DataArray[FixedField]`](../../schema/python/dataarrays.md) — the same
numpy-backed array schema as every other element type, so they get flat storage,
array access, and codegen for free. This is the case that most needs the
[type-preserving operators](../index.md#the-two-paths): fixed-point grows bits and
rounds on assignment, so working through `.val` by hand is easy to get wrong. The
operators (`*`, `+`, `-`) are sugar over the **free functions** in
[`waveflow/hw/fixpoint.py`](../../../../waveflow/hw/fixpoint.py) (not methods — the
container stays a plain container): `mult`, `add`, `sub`, `shift`, `fixed_sum`, and
`quantize`. They run entirely in the **integer domain** and match the Vitis `ap_fixed`
datapath bit-for-bit.

## Arrays of fixed-point values

`DataArray[FixedField].val` is a numpy **integer** array of *stored* values.
`from_real` quantizes real data into a format; `to_real` derives the real view:

```python
import numpy as np
from waveflow.hw.fixpoint import FixedField, from_real, to_real

Q8_4 = FixedField.specialize(8, 4)                 # ap_fixed<8, 4>
da = from_real([1.5, -2.0, 0.0625, 1.53], Q8_4)
np.asarray(da)        # array([ 24, -32,   1,  24])   <- stored ints (1.53 -> 1.5)
to_real(da)           # array([ 1.5 , -2.  ,  0.0625, 1.5 ])
```

## Arithmetic: full-precision intermediates, one explicit `quantize`

Each op reads its operands' formats, runs the vectorized integer op on the stored
arrays, and **derives the result format** per the `ap_fixed` rules — so intermediates
are *full precision and never overflow*. The **only lossy step is `quantize`**, when
you assign to a declared target format (exactly like `ap_fixed<...> y = a * b;` in
C++).

| function | result format | notes |
|----------|---------------|-------|
| `mult(a, b)` | `<Wa+Wb, Ia+Ib>` | exact product (fraction bits add) |
| `add(a, b)` | `<max(Ia,Ib)+1+max(Fa,Fb), max(Ia,Ib)+1>` | fractions aligned; one carry bit |
| `sub(a, b)` | like `add`, **signed** | subtraction may go negative |
| `shift(a, n)` | `<Wa, Ia+n>` | lossless point-move (bits unchanged), value ×2ⁿ |
| `fixed_sum(a)` | integer bits grow by `ceil(log2 N)` | full-precision reduction |
| `quantize(a, target)` | `target` | **the lossy step** — rounding (`QMode`) + overflow (`OMode`) |

```python
from waveflow.hw.fixpoint import mult, add, quantize

a = from_real([1.5, -2.0, 0.5], Q8_4)
b = from_real([2.0,  1.5, -1.0], Q8_4)

mult(a, b).element_type.cpp_type      # 'ap_fixed<16, 8, AP_TRN, AP_WRAP>'  (full precision)
to_real(mult(a, b))                    # array([ 3., -3., -0.5])  -- exact

# bring a product back to a working format (this is where rounding/overflow happen):
quantize(mult(a, b), Q8_4)             # DataArray[ap_fixed<8, 4>]
```

### Operator form (the primary spelling)

The [operators](../index.md#2-type-preserving-operators--ab--c-then-quantize) are
sugar over these functions, so a multiply-add reads like the math — and like the
HLS `ap_fixed<...> y = a*b + c;` it mirrors. The intermediates grow to full
precision; the single `quantize` is the only rounding:

```python
from waveflow.hw.fixpoint import quantize, to_real

c = from_real([0.5, 0.25, -0.5], Q8_4)

full = a * b + c                       # ap_fixed<17, 9>  -- full precision, no loss
y    = quantize(full, Q8_4)            # ap_fixed<8, 4>   -- the one explicit rounding
to_real(y)                             # array([ 3.5 , -2.75, -1.  ])
```

This is exactly the fixed case of [`examples/basic_vec`](../../../examples/basic_vec/), checked
bit-for-bit against a Vitis kernel.

## Single 64-bit dtype, fail-fast above it

All ops are numpy-vectorized over a **single 64-bit integer dtype** — `int64` for
signed formats, `uint64` for unsigned. A W-bit value fits iff `W ≤ 64`. There is **no
object-array / arbitrary-precision fallback**: any format width that exceeds 64 bits —
whether *declared* or *derived by an op* — raises `NotImplementedError` **at the
format-derivation step**, before any numpy op runs:

```python
A = FixedField.specialize(40, 20)
mult(from_real([1.0], A), from_real([1.0], A))   # NotImplementedError: derived W=80 > 64
```

This is deliberate: numpy `int64`/`uint64` *silently wrap* on overflow, which would be
an invisible bit-exactness violation, so the guard is a compile-time width check, not
a runtime hope. (Wide > 64-bit support — via Waveflow's `(n, k)` uint64-word
convention, not object arrays — is future work.) In practice, real FPGA DSP datapaths
quantize per stage and stay well under 64 bits.

## Mixed signed/unsigned is a v1 limitation

Mixing an `ap_fixed` (signed) and an `ap_ufixed` (unsigned) operand **raises**, because
numpy would coerce `int64`/`uint64` to `float64` and silently lose exactness:

```python
S = FixedField.specialize(8, 4, signed=True)
U = FixedField.specialize(8, 4, signed=False)
mult(from_real([1.5], S), from_real([2.0], U))   # NotImplementedError: mixed signed/unsigned
```

**Workaround — promote to a common signed format** with one extra integer bit to hold
the unsigned operand's MSB, then operate:

```python
S9_5 = FixedField.specialize(9, 5, signed=True)   # holds the full ap_ufixed<8,4> range
u_signed = quantize(from_real([2.0], U), S9_5)     # reinterpret the unsigned value as signed
to_real(mult(from_real([1.5], S), u_signed))       # array([3.])
```

## Worked example: a fixed-point FIR / dot product

A short FIR tap-sum is a multiply-accumulate: full-precision products, a
full-precision accumulator, then one `quantize` back to the working format — the
canonical DSP pattern, and **bit-exact with Vitis**.

```python
from waveflow.hw.fixpoint import FixedField, from_real, mult, fixed_sum, quantize, to_real

S16_8 = FixedField.specialize(16, 8)                 # ap_fixed<16, 8>
taps    = from_real([0.5, 0.25, -0.125, 0.0625], S16_8)
samples = from_real([1.0, 2.0, -1.0,    0.5],    S16_8)

prods = mult(taps, samples)        # ap_fixed<32, 16>  (full-precision products)
acc   = fixed_sum(prods)           # ap_fixed<34, 18>  (+ceil(log2 4)=2 integer bits)
y     = quantize(acc, S16_8)       # ap_fixed<16, 8>   (one quantize at the end)

to_real(y)                         # array([1.15625])   == numpy dot, exactly
```

The accumulator is sized (`fixed_sum` grows the integer bits by `ceil(log2 N)`) so the
sum is exact; the matching Vitis kernel declares an `ap_fixed<34, 18>` accumulator so
`acc += taps[i] * samples[i]` never rounds either. The
[conformance harness](../../../../examples/schemas/fixedpoint/fixedpoint_build.py) runs
exactly this `s24_12` sum-of-products (plus `mult`/`add`/`quantize`) in Vitis C-sim and
asserts the bits match the Python ops — run `pytest -m vitis -k fixedpoint`.

## See also

- [Fixed-point (FixedField)](../../schema/python/fixpoint.md) — the fixed-point *type*: the
  format, `QMode`/`OMode`, the defaults-match-Vitis contract.
- [Vectorization overview](../index.md) — the two paths and when to use each.
- [Integer vectorization](./integer.md) — the same growth-then-`quantize` story
  without a binary point.
- [Data arrays](../../schema/python/dataarrays.md) — the `DataArray` container these build on.
