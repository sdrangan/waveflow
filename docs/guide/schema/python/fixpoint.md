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
[Fixed-point vectorization](../../vectorization/fixed.md) page in the
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
[Fixed-point vectorization](../../vectorization/fixed.md)).

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
mode. If Python and Vitis ever disagree, the Python model is wrong — it is fixed, the
comparison is never loosened. Run it with `pytest -m vitis -k fixedpoint`.

## See also

- [Fixed-point vectorization](../../vectorization/fixed.md) — the compute:
  `DataArray[FixedField]`, the `a*b + c` operators / `mult`/`add`/`quantize`,
  result-format propagation, and a worked FIR.
- [Data Fields and Lists](./datalists.md) — the `IntField` base and serialization.
