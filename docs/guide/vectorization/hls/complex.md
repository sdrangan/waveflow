---
title: "Vitis: complex arrays"
parent: HLS
grand_parent: Vectorization
nav_order: 3
audience: hls
applies_to: [ComplexField]
api: [read_array_lane, read_array_slice]
summary: "Vectorized complex arrays in Vitis — the std::complex / wf_cint element storage and the lane loop over complex values, building on the raw-array pattern."
---

# Vectorized arrays in Vitis — complex

Complex samples are the workhorse of wireless signal processing — IQ data, channel estimates, beamforming
weights. Waveflow's `ComplexField` is a first-class element type, which means **a complex array serializes
and vectorizes through exactly the same machinery as a scalar array** — the `<type>_array_utils` helpers
from [raw](./raw.md) / [struct](./struct.md) — with complex arithmetic supplied by a generated
`complex_utils.hpp`. This page is the complex-specific walkthrough; for the Python-side numpy model see
[Complex vectorization](../python/complex.md).

## The complex element type

A `ComplexField` over a scalar inner field maps to a C++ element type:

| inner | C++ `cpp_type` |
|---|---|
| `FloatField` | `std::complex<float>` / `std::complex<double>` |
| `FixedField` | `std::complex<ap_fixed<…>>` |
| `IntField`   | `wf_cint<W>` — a 2-`ap_int` struct (`std::complex<ap_int>` is non-standard) |

Each element is an interleaved `(re, im)` pair of `data_bw`-bit components, so its width is `2·data_bw` and
its packing factor over a `WORD_BW` channel is `pf = WORD_BW / (2·data_bw)`.

## Code generation

Generate the complex element's packing the same way as any element — name the `ComplexField` type:

```python
from waveflow.hw.complexfield import ComplexField
from waveflow.hw.fixpoint import FixedField

CFixed = ComplexField.specialize(FixedField.specialize(16, 4, signed=True))

dag.add(ArrayUtilsStep(CFixed, [64, 128]))    # generates <type>_array_utils.h for the complex element
```

This emits the usual `<type>_array_utils::` namespace (`pf<>` / `lane_capacity<>`, `read_array_slice` /
`read_array_lane`, and the write/stream variants) — complex elements pack/unpack like any other, re in the low
`data_bw` bits and im in the high `data_bw` bits of each slot. The arithmetic comes from two headers shipped
with Waveflow:

- **`complex_utils.hpp`** — `cmult` / `cadd` / `csub` / `conj` over the complex `cpp_type`, the explicit
  re/im formula at full precision (not `std::complex operator*`, which would FMA-contract / requantize).
- **`wf_cint.h`** — the integer-inner complex struct.

## Reading, computing, writing

Because the element is first-class, the kernel is the same shape as the scalar [raw](./raw.md) case —
just a complex element type and `complex_utils::` arithmetic. A complex multiply over two arrays:

```cpp
#include "cfixed_array_utils.h"
#include "complex_utils.hpp"
namespace au = cfixed_array_utils;

au::value_type a[N], b[N], y[N];               // complex elements (std::complex<ap_fixed>)
au::read_array_slice<WORD_BW>(a_words, a);     // whole array resident (static-size overload)
au::read_array_slice<WORD_BW>(b_words, b);

for (int i = 0; i < N; ++i) {
#pragma HLS PIPELINE II=1
    y[i] = complex_utils::cmult(a[i], b[i]);   // full-precision complex multiply
}

au::write_array_slice<WORD_BW>(y, y_words, 0, N);
```

`conj` / `cadd` / `csub` follow the same shape (`complex_utils::conj(a[i])`, etc.). Call them **qualified**
(`complex_utils::conj`): an unqualified `conj` on a `std::complex` argument resolves to `std::conj` via ADL,
which is not the full-precision Waveflow operator.

For lane-level throughput, the [raw lane loop](./raw.md#the-lane-loop)
works unchanged — `read_array_lane<WORD_BW>` delivers `LW` complex lanes per word and you unroll
`complex_utils::cmult` across them. Note that because a complex element is `2·data_bw` bits, a *real* array
of the same `data_bw` packs **twice** the lanes per word — the real-vs-complex throughput difference is just
the packing factor.

## Worked example

`examples/schemas/complex` is the bit-exact conformance: it lays operands out with `arrayutils.write_array`,
runs `cmult` / `cadd` / `csub` / `conj` kernels built from the `<type>_array_utils` serialization helpers +
`complex_utils.hpp`, and checks the C++ words against the Python `DataArray[ComplexField]` model —
bit-for-bit, on real Vitis.

## See also

- [Complex vectorization](../python/complex.md) — the Python-side numpy complex model.
- [`raw`](./raw.md) — the packing factor and lane loop (apply to complex unchanged).
- [Serialization](../../schema/hls/serialization.md) — the schema-level packing model.
