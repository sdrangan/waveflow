---
title: Fixed-point (FixedField)
parent: Python
grand_parent: Data Schemas
nav_order: 5
audience: python
applies_to: [FixedField]
api: [FixedField]
summary: "FixedField — ap_fixed-compatible fixed-point: total width, integer bits, signedness, and the rounding/overflow modes."
---

# Fixed-Point: `FixedField`

`FixedField` is a **Vitis-bit-exact** fixed-point scalar type. It maps to Vitis HLS
`ap_fixed<W, I, Q, O>` (or `ap_ufixed<...>`) and its Python value model reproduces the
hardware **bit-for-bit** — for quantization *and* arithmetic. It is defined in
[`waveflow/hw/fixpoint.py`](../../../../waveflow/hw/fixpoint.py); vector operations (the
**compute**) are covered on the
[Fixed-point vectorization](../../vectorization/python/fixed.md) page in the
[Vectorization](../../vectorization/) section.

## The `ap_fixed` model

An `ap_fixed<W, I>` value is a **`W`-bit integer with an implied binary point** `I`
bits from the MSB. With `F = W − I` fractional bits, the real value is

```
value = stored · 2^(−F)
```

where `stored` is the W-bit two's-complement integer (`ap_fixed`, signed) or unsigned
integer (`ap_ufixed`). `I` counts the integer bits (sign-inclusive when signed); `F`
the fractional bits. For example `ap_fixed<8, 4>` has `F = 4`, so its LSB is
`2^−4 = 0.0625` and its range is `[−8, 7.9375]`.

## Declaring a format

`FixedField.specialize` returns a cached element class — **one class per distinct
format**:

```python
from waveflow.hw.fixpoint import FixedField
from waveflow.utils.fixputils import QMode, OMode

Q8_4 = FixedField.specialize(8, 4)                       # ap_fixed<8, 4, AP_TRN, AP_WRAP>
U8_4 = FixedField.specialize(8, 4, signed=False)         # ap_ufixed<8, 4, AP_TRN, AP_WRAP>
Q16_8 = FixedField.specialize(16, 8,
                              q_mode=QMode.AP_RND,
                              o_mode=OMode.AP_SAT)        # ap_fixed<16, 8, AP_RND, AP_SAT>
```

The parameters are `W` (total bits), `I` (integer bits), `signed` (`ap_fixed` vs
`ap_ufixed`), and the quantization / overflow modes. The emitted C++ type is on the
class:

```python
>>> Q16_8.cpp_type
'ap_fixed<16, 8, AP_RND, AP_SAT>'
```

**Defaults match Vitis.** A default-constructed format uses `signed = True`,
`AP_TRN`, and `AP_WRAP` — exactly Vitis's `ap_fixed` defaults — so
`FixedField.specialize(W, I)` already matches the default hardware type.

A single `FixedField` is a scalar; arrays use `DataArray[FixedField]` (see
[Fixed-point vectorization](../../vectorization/python/fixed.md)).

## Quantization (`QMode`) and overflow (`OMode`) modes

The modes are `enum.Enum`s whose value is the Vitis template token (so codegen emits
`q.value` / `o.value` directly). The v1 subset:

| `QMode` | meaning |
|---------|---------|
| `AP_TRN` *(default)* | truncate — round toward **−∞** (floor), for positives **and** negatives |
| `AP_RND` | **round half up** — round to nearest, ties toward **+∞** |

| `OMode` | meaning |
|---------|---------|
| `AP_WRAP` *(default)* | two's-complement wrap-around (mask) |
| `AP_SAT` | saturate — clip to the format's `[min, max]` (asymmetric for signed) |

### Round-half-up vs round-half-even

`AP_RND` is **round-half-up** (ties go toward +∞), matching Vitis `AP_RND`. It is
*not* the unbiased banker's rounding (round-half-to-**even**) — that is Vitis
`AP_RND_CONV`, which is **not in v1** (planned, see Phase 6). If your model needs
unbiased rounding, do not assume `AP_RND` provides it.

## Division

`div(a, b)` is `ap_fixed` division, as Vitis 2024.1 and 2025.1 implement it in
`ap_fixed_base.h` (bit-exact against both: `tests/hw/test_fixpoint_vitis.py`), plus a guard that
Waveflow adds:

- **Result format.** `<Sb + Wa + max(Fb, 0), Sb + Ia + Fb>`, where `Sb` is 1 for a
  signed divisor. The quotient keeps the **dividend's** fraction bits.
- **Rounding.** The stored quotient is `(a << Fb) / b` in integer division,
  truncated **toward zero**: `−1/3` gives `−0.3125` in `s8_4`, not floor's
  `−0.375`.
- **Precision.** To get a more precise quotient, first `quantize` the dividend to
  more fraction bits. That step is exact.
- **Zero divisor.** `ap_fixed` leaves `x / 0` undefined, so Waveflow defines it as
  **0**. Matching C++ must guard the same way: `(b == 0) ? 0 : a / b`.
- **One overflow corner.** Vitis C-simulation divides at the shifted dividend's
  width `max(Wa + Fb, Wb)`, not at the wider result type, so the most negative
  dividend divided by −1 LSB wraps back to the most negative value. `div`
  reproduces this exactly.

```python
from waveflow.hw.fixpoint import FixedField, div, from_real, to_real

Q8_4 = FixedField.specialize(8, 4)
q = div(from_real([1.0, -1.0, 1.0], Q8_4), from_real([3.0, 3.0, 0.0], Q8_4))
q.element_type.cpp_type   # 'ap_fixed<13, 9, AP_TRN, AP_WRAP>'
to_real(q)                # array([ 0.3125, -0.3125,  0.    ])
```

## Magnitude

`abs(a)` in [`waveflow/hw/fixpoint.py`](../../../../waveflow/hw/fixpoint.py) (also the
builtin `abs()` on a `DataArray`) is the full-precision magnitude:

- **Result format.** A signed `<W, I>` gives `<W+1, I+1>`, the type of `ap_fixed`
  unary minus, so `|min|` fits. An unsigned format is returned unchanged.
- **Matching C++.** `ap_fixed<W+1, I+1> m = (a < 0) ? ap_fixed<W+1, I+1>(-a) : ap_fixed<W+1, I+1>(a);`
  -- checked bit for bit in Vitis C-simulation by `test_ap_fixed_abs_kernel_csim_bit_exact`
  in [`tests/hw/test_fixpoint_vitis.py`](../../../../tests/hw/test_fixpoint_vitis.py).
- **Back to the input width.** `quantize(abs(a), T)`; with `AP_SAT`, `|min|` saturates to
  the maximum. Vitis `hls::abs` on `ap_fixed<W, I>` keeps the width and returns 0 for
  `min`, so it is a different op.

```python
from waveflow.hw.fixpoint import abs as fabs

m = fabs(from_real([-8.0, 3.5], Q8_4))
m.element_type.cpp_type   # 'ap_fixed<9, 5, AP_TRN, AP_WRAP>'
to_real(m)                # array([8. , 3.5])
```

## Comparison

`a < b`, `a <= b`, `a > b` and `a >= b` on fixed-point arrays compare **exactly**, as `ap_fixed`
does in C++. Any two formats can be compared, including a signed one with an unsigned one, which
the arithmetic above refuses. The functions `lt`, `le`, `gt`, `ge`, `eq` and `ne` in
[`waveflow/hw/fixpoint.py`](../../../../waveflow/hw/fixpoint.py) do the same, and each side can be an
array (broadcast as NumPy does) or a single `FixedField` value:

```python
from waveflow.hw.fixpoint import FixedField, eq, from_real

Sample = FixedField.specialize(12, 1)                  # ap_fixed<12, 1>
Thresh = FixedField.specialize(16, 4, signed=False)    # ap_ufixed<16, 4>

x = from_real([0.0, 0.5, -0.75, 0.25, 0.3125], Sample)
thresh = Thresh(int(0.25 * 2**12))                     # a stored integer, as a field read gives it
x > thresh                 # array([False,  True, False, False,  True])
eq(from_real([0.5], Sample), from_real([0.5], Thresh))   # array([ True]): equal in value
```

- **How.** Both stored integers are shifted to the finer of the two fraction lengths, in a signed
  integer wide enough for either, and compared. There is no rounding and no float, so the result is
  exact for every format, including 64-bit ones.
- **Matching C++.** `x > thresh` on an `ap_fixed<12, 1>` and an `ap_ufixed<16, 4>` is the same
  comparison.
- **A plain number is refused.** `x > 0.25` raises a `TypeError`, because a plain number could be a
  stored integer or a real value. Wrap a stored integer as `Thresh(stored)`, or quantize a real
  value with `from_real(0.25, Thresh)`.
- **`==` and `!=` are functions only.** On a `DataArray`, `==` stays identity, so arrays stay
  hashable. Compare values with `eq` and `ne`.

## Integer-backed storage and the real view

Storage is the **stored integer** (decision: integer-backed). A scalar `FixedField`'s
`.val` is the stored W-bit integer; `.real` derives the real value:

```python
f = Q8_4(24)        # assign the *stored* integer
f.val               # 24  (np.int64)
f.real              # 1.5  (= 24 · 2^-4)
```

To quantize a **real** value into a format, use `from_real` (which returns a
`DataArray[FixedField]` — see the vector page); assigning a non-integer real directly
to a scalar `FixedField` raises, to keep "stored integer" and "real value"
unambiguous.

`from_real(values, T)` and `quantize(a, T)` return an array of `T` itself, so what `T` carries
beyond its format comes along. For example, `T = FixedField.specialize(12, 1, include_dir="include")`
tells the array utilities where to put their header. An operation such as `a * b` has a new format,
and its result is an array of the plain class of that format. The array and file helpers
(`write_array`, `write_uint32_file` and the rest) accept either class wherever the format is the same.

`FixedField` subclasses `IntField` and reuses its W-bit word
[serialization](./datalists.md) unchanged — the stored integer *is* the payload; only
the C++ *typed view* (`ap_fixed` instead of `ap_int`, via a `.range()`
bit-reinterpret) differs.

## Bit-exactness — the conformance harness

The contract is proven empirically. The harness under
[`examples/schemas/fixedpoint/`](../../../../examples/schemas/fixedpoint/fixedpoint_build.py)
quantizes a sweep of edge values (exact-representable, rounding midpoints, min/max
overflow, negatives, unsigned-negative inputs) in Vitis C-sim and asserts the emitted
bits equal the Python `fixputils` bits **exactly**, across the curated configs × every
mode. The arithmetic cases include division: 2632 operand pairs over five format pairs
and all four modes, with zero, tiny and negative divisors. If Python and Vitis ever disagree, the Python model is wrong — it is fixed, the
comparison is never loosened. Run it with `pytest -m vitis -k fixedpoint`.

## See also

- [Fixed-point vectorization](../../vectorization/python/fixed.md) — the compute:
  `DataArray[FixedField]`, the `a*b + c` operators / `mult`/`add`/`quantize`,
  result-format propagation, and a worked FIR.
- [Data Fields and Lists](./datalists.md) — the `IntField` base and serialization.
