---
title: Complex vectorization
parent: Python
grand_parent: Vectorization
nav_order: 5
audience: python
applies_to: [ComplexField]
api: [ComplexField]
summary: "The complex vectorized model — ComplexField arrays, complex arithmetic (cmult/cadd/conj), and the NumPy-vs-hardware complex-multiply edge."
---

# Complex Vectorization

This is the [vectorization](../index.md) story for complex — the **compute**. The complex
*type* itself (the per-inner value representation, the C++ `std::complex` / `wf_cint`
mapping, the bit-exactness contract) lives on the
[ComplexField type page](../../schema/python/complex.md); read that first if you haven't.

Complex arrays use [`DataArray[ComplexField]`](../../schema/python/dataarrays.md), generic over a scalar
inner field — `FloatField`, `FixedField`, or `IntField`. The key idea for *speed*: complex
arithmetic **composes the inner field's own vectorized ops** on the real and imaginary
components, so a complex array stays as loop-free and NumPy-fast as a real one — and stays
**bit-exact with the matching Vitis complex type**. For a fixed/int inner, that means complex
math is just integer NumPy over the stored components; it inherits the `ap_fixed` growth rules
and the single-64-bit guard from [`FixedField`](../../schema/python/fixpoint.md), with no fixed-point math
reimplemented.

## Arrays of complex values

The `.val` representation depends on the inner, because **numpy has no integer-complex dtype**:

- **float inner → native numpy complex** — `.val` is a `complex64`/`complex128` array, and a
  complex op is a numpy complex op.

  ```python
  import numpy as np
  from waveflow.hw.complexfield import ComplexField
  from waveflow.hw.dataschema import DataArray, FloatField

  CFlt = ComplexField.specialize(FloatField.specialize(32))    # std::complex<float>
  af = DataArray.specialize(CFlt, max_shape=(2,))(np.array([1 + 2j, 3 - 4j], np.complex64))
  af.val                       # array([1.+2.j, 3.-4.j], dtype=complex64)
  ```

- **fixed / int inner → a numpy *structured* dtype** `[('re', D), ('im', D)]` of the **stored
  integers**. `v['re']` / `v['im']` are int-array views, so each complex op runs as integer
  NumPy over whole component arrays — still vectorized, still loop-free.

  ```python
  from waveflow.hw.complexfield import ComplexField
  from waveflow.hw.fixpoint import FixedField, Format
  from waveflow.hw.dataschema import DataArray
  from waveflow.utils import complexutils as cx

  CFix = ComplexField.specialize(FixedField.specialize(16, 8))   # std::complex<ap_fixed<16,8>>
  ax = DataArray.specialize(CFix, max_shape=(2,))(cx.make_complex([24, -16], [8, -8], Format(16, 8, True)))
  ax.val["re"]                 # array([ 24, -16])   (= 1.5, -1.0 in ap_fixed<16,8>)
  ax.val["im"]                 # array([  8,  -8])
  ```

## Arithmetic: full-precision growth, composed on the components

The [operators](./numerical.md#type-preserving-operators) `*` / `+` / `-` (and the free function `conj`) are
**type-preserving**: each runs the inner field's vectorized op on the real/imag components and
**derives the result inner format** with full precision. For a fixed/int inner the formats
follow the `ap_fixed` rules — products widen, sums grow an integer bit — so intermediates never
overflow:

| op | result inner format | example (`ap_fixed<16,8>`) |
|----|--------------------|----------------------------|
| `cmult` (`*`) | `(2W+1, 2I+1, signed)` | `ap_fixed<33, 17>` |
| `cadd` (`+`)  | `(W+1, I+1)`           | `ap_fixed<17, 9>` |
| `csub` (`-`)  | `(W+1, I+1, signed)`   | `ap_fixed<17, 9>` |
| `conj`        | `(W+1, I+1, signed)`   | `ap_fixed<17, 9>` |

```python
from waveflow.hw.complexfield import conj

bx = DataArray.specialize(CFix, max_shape=(2,))(cx.make_complex([10, 4], [-6, 2], Format(16, 8, True)))

(ax * bx).element_type.inner_type.cpp_type    # 'ap_fixed<33, 17, AP_TRN, AP_WRAP>'  (full precision)
(ax + bx).element_type.inner_type.cpp_type    # 'ap_fixed<17, 9, AP_TRN, AP_WRAP>'
conj(ax).element_type.inner_type.cpp_type     # 'ap_fixed<17, 9, AP_TRN, AP_WRAP>'
```

`cmult` and `conj` produce signed results (a difference of products / a negated imaginary part),
so they require a **signed inner** — an unsigned inner raises. Mixed signed/unsigned operands
raise too (inherited from the [fixed-point rules](./fixed.md#mixed-signedunsigned-is-a-v1-limitation)),
and any derived width above 64 bits fails fast at the format-derivation step, exactly as for
real fixed-point. For a **float** inner the result is the same float type (no growth).

### The float complex-multiply edge

For a float inner, `cmult` does **not** use numpy's complex-dtype `*` operator: numpy's complex
multiply is FMA-based, while Vitis HLS `std::complex<float>` evaluates the **naive**
`(ar·br − ai·bi)` formula. `ComplexField` follows the naive, hardware-faithful formula so it
stays bit-exact with Vitis on rounding-triggering operands; raw numpy FMA semantics remain
available via `.val`. The full story is on the
[ComplexField type page](../../schema/python/complex.md#the-float-complex-multiply-edge).

### Rounding back to a working format

There is no single complex `quantize`: rounding is done **per component** through the inner
field, since complex arithmetic composes that field. Take the grown result's components and
`quantize` each with [`FixedField`](../../schema/python/fixpoint.md)'s `quantize`, then recombine:

```python
import numpy as np
from waveflow.hw.fixpoint import FixedField, quantize

Q = FixedField.specialize(16, 8)
prod = ax * bx                              # ap_fixed<33, 17> components
G    = prod.element_type.inner_type         # the grown inner format
re_q = quantize(DataArray.specialize(G, max_shape=(2,))(prod.val["re"]), Q)
im_q = quantize(DataArray.specialize(G, max_shape=(2,))(prod.val["im"]), Q)
out  = DataArray.specialize(CFix, max_shape=(2,))(
    cx.make_complex(list(np.asarray(re_q)), list(np.asarray(im_q)), Format(16, 8, True)))
```

## Worked example: a radix-2 FFT butterfly

A radix-2 butterfly is `(a + w·b, a − w·b)` for a twiddle `w` — one `cmult` and a `cadd`/`csub`,
all vectorized over the lanes and **bit-exact with `std::complex<ap_fixed>`**. Stored integers
are `value × 2⁸` in `ap_fixed<16,8>` (so `256` is `1.0`, `181 ≈ 0.707`):

```python
from waveflow.hw.complexfield import ComplexField
from waveflow.hw.fixpoint import FixedField, Format
from waveflow.hw.dataschema import DataArray
from waveflow.utils import complexutils as cx

CFix = ComplexField.specialize(FixedField.specialize(16, 8))
def cfix(re, im):
    return DataArray.specialize(CFix, max_shape=(len(re),))(cx.make_complex(re, im, Format(16, 8, True)))

a = cfix([256, -128], [128,   64])     # 1 + 0.5j,  -0.5 + 0.25j
w = cfix([181,    0], [-181, 256])     # 0.707 - 0.707j,  0 + 1j   (twiddles)
b = cfix([256,  256], [0,   -128])     # 1 + 0j,  1 - 0.5j

wb  = w * b                            # ap_fixed<33, 17> products (full precision)
top = a + wb                           # ap_fixed<34, 18>  -> a + w·b
bot = a - wb                           # ap_fixed<34, 18>  -> a - w·b
top.element_type.inner_type.cpp_type   # 'ap_fixed<34, 18, AP_TRN, AP_WRAP>'
```

The products and sums grow to full precision (no overflow, no rounding); a hardware butterfly
that declares the same widened `ap_fixed` intermediates computes the identical bits. The
[conformance harness](../../../../examples/schemas/complex/complex_build.py) runs `cmult` / `cadd`
/ `csub` / `conj` (fixed, int, and float — including the float multiply edge) in Vitis C-sim and
asserts the emitted bits equal these Python ops — run `pytest -m vitis -k complex`.

## See also

- [Complex (ComplexField)](../../schema/python/complex.md) — the complex *type*: value representations,
  the C++ mapping, and the bit-exactness contract.
- [Fixed-point vectorization](./fixed.md) — the inner field's growth-then-`quantize` story that
  complex composes for a fixed/int inner.
- [Vectorization overview](../index.md) — the two paths and when to use each.
- [Data arrays](../../schema/python/dataarrays.md) — the `DataArray` container these build on.
