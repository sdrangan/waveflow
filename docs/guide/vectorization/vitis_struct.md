---
title: "Vitis: struct arrays"
parent: Vectorization
nav_order: 11
---

# Vectorized arrays in Vitis — `struct` storage

The default `cpp_storage` for a `DataArray` is `"struct"`. Where [`raw` mode](./vitis_raw.md) gives you a
flat C array and the free-function packing helpers for explicit lane control, **`struct` mode wraps the
array in a generated type whose methods do the packing for you** — the whole array in, the whole array out,
the lanes hidden. Reach for it when the array is a *field* of a larger schema, a port payload, or an object
you pass around, and you don't need cycle-level control of the loop.

## Code generation in `struct` mode

A `struct`-mode `DataArray` lowers to a generated struct — a `data[N]` member plus serialization methods —
emitted by `DataSchemaStep` as part of the schema's C++ type (the same build flow as any schema header; see
[Code Generation](../schema/hls/codegen.md)). The element's packing still comes from `ArrayUtilsStep`: the
struct's methods **delegate to the element's `<elem>_array_utils` free functions**, so both storage modes
share one packing implementation. A generated array struct looks like:

```cpp
struct Float32Array {
    float data[256];
    template<int WORD_BW> void read_array(const ap_uint<WORD_BW> x[]);    // words → data
    template<int WORD_BW> void write_array(ap_uint<WORD_BW> x[]) const;   // data → words
    // ... stream variants (read_stream / read_axi4_stream / ...)
};
```

## Declaring and using the struct in C++

Declare an instance, call its serialize/deserialize methods to move the **whole array** over a channel, and
read or write elements through the `data` member:

```cpp
#include "include/sample_array.h"

Float32Array samples;
samples.read_array<WORD_BW>(words);          // deserialize all 256 elements from packed words

for (int i = 0; i < 256; ++i) {
    samples.data[i] = samples.data[i] * samples.data[i];   // y = x*x
}

samples.write_array<WORD_BW>(out_words);     // serialize back
```

The packing factor and word layout are identical to `raw` mode (`pf = WORD_BW / element_bits`) — the struct
just keeps them under the hood. Like every Waveflow serializer the methods are templated on `WORD_BW`, so
retargeting the channel width is a one-constant change, and the bytes match the Python golden exactly.

## `struct` vs `raw`: which to use

| | `struct` | `raw` |
|---|---|---|
| C++ shape | a wrapper struct (`data[N]` + methods) | a flat `elem_t[N]` |
| Packing | hidden inside the methods | explicit (`read_array_lane` / `read_array_slice` free functions) |
| Lane control | no | yes — the unrolled lane loop |
| Best for | a field of a schema, a port payload, whole-array I/O | throughput kernels, per-cycle lane scheduling |

If you find yourself wanting to unroll across lanes (the throughput pattern), reach for
[`raw`](./vitis_raw.md). If you just want to move an array in and out as one typed object, `struct` is the
simpler default.

## See also

- [`vitis_raw.md`](./vitis_raw.md) — the flat array, lane loop, and throughput pattern.
- [Serialization](../schema/hls/serialization.md) — the schema-level packing model.
- [Data Arrays](../schema/python/dataarrays.md) — declaring `DataArray` and `cpp_storage`.
