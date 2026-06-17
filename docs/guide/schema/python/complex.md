---
title: Complex (ComplexField)
parent: Python
grand_parent: Data Schemas
nav_order: 6
audience: python
applies_to: [ComplexField]
api: [ComplexField]
summary: "ComplexField — a complex element over a scalar inner field (re/im), the interleaved layout and complex arithmetic helpers."
---

# Complex: `ComplexField`

`ComplexField` is a complex scalar type **generic over a scalar inner field** —
`FloatField`, `FixedField`, or `IntField`. The real and imaginary parts share one inner
format, and the Python value model is **bit-exact with the corresponding Vitis complex
type** for representation *and* arithmetic. It is defined in
[`waveflow/hw/complexfield.py`](../../../../waveflow/hw/complexfield.py) and is the
foundation for complex DSP (the bit-exact FFT model and friends).

It composes the [fixed-point](./fixpoint.md) machinery: complex arithmetic over a
fixed/int inner lowers to the inner field's own integer arithmetic on the real/imag
components, so it inherits the `ap_fixed` result-format rules and the bit-exactness
guarantee — no fixed-point math is reimplemented.

## Declaring a `ComplexField`

`ComplexField.specialize(inner)` returns a cached element class — one per inner field
class. The inner carries the per-component format:

```python
from waveflow.hw.complexfield import ComplexField
from waveflow.hw.fixpoint import FixedField
from waveflow.hw.dataschema import FloatField, IntField

CFix = ComplexField.specialize(FixedField.specialize(16, 8))   # complex ap_fixed<16,8>
CFlt = ComplexField.specialize(FloatField.specialize(32))      # complex float
CInt = ComplexField.specialize(IntField.specialize(16))        # complex int16
```

The emitted C++ type is on the class:

```python
>>> CFix.cpp_type
'std::complex<ap_fixed<16, 8, AP_TRN, AP_WRAP>>'
>>> CFlt.cpp_type
'std::complex<float>'
>>> CInt.cpp_type
'wf_cint<16>'
```

A single `ComplexField` is a scalar; arrays use `DataArray[ComplexField]` (reusing the
[`DataArray`](./dataarrays.md) machinery — no parallel class).

## Value representation (per inner)

The Python `.val` representation depends on the inner, because **numpy has no
integer-complex dtype**:

- **float inner → native numpy complex** (`complex64` for `float32`, `complex128` for
  `float64`). `.real` / `.imag` are floats.

  ```python
  import numpy as np
  from waveflow.hw.dataschema import DataArray

  af = DataArray.specialize(CFlt, max_shape=(2,))(np.array([1 + 2j, 3 - 4j], dtype=np.complex64))
  af.val            # array([1.+2.j, 3.-4.j], dtype=complex64)
  ```

- **fixed / int inner → a numpy *structured* dtype** `[('re', D), ('im', D)]` of the
  **stored integers** (`D` is `int64` signed / `uint64` unsigned) — the "custom type".
  `v['re']` / `v['im']` are int-array views, so it stays vectorized and loop-free, and
  (like numpy complex) it is interleaved re/im in memory.

  ```python
  from waveflow.utils import complexutils as cx
  from waveflow.utils.fixputils import Format

  v = cx.make_complex([24, -16], [8, -8], Format(16, 8, True))   # stored integers
  ax = DataArray.specialize(CFix, max_shape=(2,))(v)
  ax.val["re"]      # array([ 24, -16])   (= 1.5, -1.0 in ap_fixed<16,8>)
  ax.val["im"]      # array([  8,  -8])
  ```

  As with [`FixedField`](./fixpoint.md), storage is the **stored integer**, not the
  real value; quantize real components with the inner's quantization (`fixpoint.from_real`
  on re/im) and recombine.

## Interleaved I/Q layout

Serialization is **interleaved I/Q** — `inner.serialize(re)` then `inner.serialize(im)`,
total width `2 × inner` — implemented by *composing the inner field's own*
[serialization](./datalists.md) (so the float IEEE bit-view and wide-int multi-word
packing are reused verbatim). This is exactly the contiguous `[real, imag]` layout that
C++ `std::complex<T>` uses, so a `DataArray[ComplexField]` maps **directly onto a
`std::complex<T>` array** with no repacking.

## C++ mapping

| inner | C++ type |
|-------|----------|
| float | `std::complex<float>` / `std::complex<double>` |
| fixed | `std::complex<ap_fixed<W, I, Q, O>>` — the synthesizable canonical |
| int   | a Waveflow-emitted `wf_cint<W>` struct (two `ap_int<W>`) |

`std::complex` is only specified for floating-point types, so **int-complex is Waveflow's
own struct** (`wf_cint`) rather than the non-standard `std::complex<ap_int>`. Waveflow
owns the general struct and adapts to `std::complex<ap_fixed>` at the DSP-library
boundary.

## Arithmetic

Complex arithmetic is exposed as **type-preserving operators** (full-precision growth),
sugar over the underlying `cmult` / `cadd` / `csub` functions; `conj` is a free function:

```python
from waveflow.hw.complexfield import conj

bx = DataArray.specialize(CFix, max_shape=(2,))(cx.make_complex([10, 4], [-6, 2], Format(16, 8, True)))
prod = ax * bx        # cmult
sm   = ax + bx        # cadd
df   = ax - bx        # csub
cj   = conj(ax)       # conjugate
```

For a **fixed/int** inner, each operation composes the inner field's integer arithmetic
on the real/imag components, so the result inner format follows the
[`FixedField`](./fixpoint.md) rules (`P = a·b` widens, sums grow by an integer bit) — and
inherits the single-64-bit dtype with a **fail-fast >64-bit guard**:

| op | result inner format | example (`ap_fixed<16,8>`) |
|----|--------------------|----------------------------|
| `cmult` (`*`) | `(2W+1, 2I+1, signed)` | `ap_fixed<33, 17>` |
| `cadd` (`+`)  | `(W+1, I+1)` | `ap_fixed<17, 9>` |
| `csub` (`-`)  | `(W+1, I+1, signed)` | `ap_fixed<17, 9>` |
| `conj`        | `(W+1, I+1, signed)` | `ap_fixed<17, 9>` |

```python
>>> (ax * bx).element_type.inner_type.cpp_type
'ap_fixed<33, 17, AP_TRN, AP_WRAP>'
>>> (ax + bx).element_type.inner_type.cpp_type
'ap_fixed<17, 9, AP_TRN, AP_WRAP>'
```

`cmult` and `conj` produce signed results (a difference of products / a negated imag), so
they require a **signed inner** (an unsigned inner raises); `cadd` keeps the inner's
signedness. Mixed signed/unsigned operands raise (inherited from the fixed-point rules).
Rounding stays an explicit `quantize` on the inner; `.val` is the numpy escape hatch. For
a **float** inner the result is the same float type (no growth).

## The float complex-multiply edge

For a float inner, `cmult` does **not** use numpy's complex-dtype `*` operator. numpy's
complex multiply is **FMA-based** and diverges from the hardware's naive
`(ar·br − ai·bi) + j(ar·bi + ai·br)` formula on a large fraction of operands:

```python
import numpy as np
from waveflow.hw.complexfield import cmult

rng = np.random.default_rng(0)
a = (rng.standard_normal(1000) + 1j * rng.standard_normal(1000)).astype(np.complex64)
b = (rng.standard_normal(1000) + 1j * rng.standard_normal(1000)).astype(np.complex64)
da = DataArray.specialize(CFlt, max_shape=(1000,))
model = cmult(da(a), da(b)).val
fma = a * b                                   # raw numpy: FMA-based
int((model != fma).sum())                     # 418 — they genuinely differ
```

Vitis HLS `std::complex<float>`/`<double>` evaluate the **naive** formula (no FMA
contraction in csim), so `ComplexField`'s float `cmult` uses that hardware-faithful naive
formula — three IEEE-rounded float ops per component, in libstdc++'s `operator*` order —
making it **bit-exact with Vitis** (confirmed on random rounding-triggering operands). Raw
numpy FMA semantics remain available via `.val` when you want them; `cadd` / `csub` /
`conj` are componentwise and already match `std::complex` exactly.

## Bit-exactness — the conformance harness

The contract is proven empirically. The harness under
[`examples/schemas/complex/`](../../../../examples/schemas/complex/complex_build.py)
generates a complex kernel per case, runs it in Vitis C-sim, and asserts the emitted
stored bits equal the Python `DataArray[ComplexField]` bits **exactly** — for round-trip
*and* `cmult` / `cadd` / `csub` / `conj`, per inner:

- **fixed** vs `std::complex<ap_fixed>` (signed configs; unsigned round-trip + `cadd`),
- **int** vs the `wf_cint` struct (`s8` / `s16`),
- **float** vs `std::complex<float>`/`<double>` — including the multiply edge above, on
  random rounding-triggering operands.

If Python and Vitis ever disagree, the Python model is wrong — it is fixed, the comparison
is never loosened. Run it with `pytest -m vitis -k complex`.

## See also

- [Fixed-point (`FixedField`)](./fixpoint.md) — the integer-backed inner whose arithmetic
  `ComplexField` composes for the fixed/int case.
- [Fixed-point vectorization](../../vectorization/python/fixed.md) and the
  [Vectorization](../../vectorization/) section — `DataArray` compute and result-format
  propagation (a complex-vectorization page is a planned follow-on).
- [Data Fields and Lists](./datalists.md) — the scalar field bases and the serialization
  that `ComplexField` composes for its interleaved I/Q layout.
