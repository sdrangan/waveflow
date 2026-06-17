---
title: "Vitis: raw arrays"
parent: HLS
grand_parent: Vectorization
nav_order: 1
audience: hls
api: [pf, lane_capacity, get_nwords, read_array_lane, write_array_lane, read_array_slice, write_array_slice]
summary: "Vectorized arrays in raw storage — a flat C array; the packing factor and lanes, read_array_slice over a local buffer, and the canonical lane loop (with the pf=0 wide-element case)."
---

# Vectorized arrays in Vitis — `raw` storage

Just as each [data schema](../../schema/hls/codegen.md) has a bit-exact Vitis C++ counterpart, so does each
`DataArray`: Waveflow generates the include files and helper methods that let a synthesizable kernel
manipulate the array — pack it, unpack it, process it lane-by-lane — using the *same* layout the Python
model uses.

A `DataArray` lowers to C++ in one of two **storage modes** (see [Data Arrays](../../schema/python/dataarrays.md)).
This page covers **`raw` mode** — a flat C array — the one you reach for when you want explicit, per-cycle
control of the vectorized loop. The [`struct`](./struct.md) mode (a wrapper with methods) and the
[`complex`](./complex.md) element are covered separately.

## Code generation in `raw` mode

The packing helpers are generated from the array's **element type** — and that element can be *any*
`DataSchema` scalar (`IntField`, `FixedField`, `FloatField`, `ComplexField`). You name the element type
and the channel widths you intend to support; everything else follows. For a 32-bit float element:

```python
from waveflow.build.build import BuildConfig, BuildDag
from waveflow.build.streamutils import StreamUtilsStep
from waveflow.hw.arrayutils import ArrayUtilsStep
from waveflow.hw.dataschema import FloatField

Float32 = FloatField.specialize(32)

cfg = BuildConfig(root_dir=project_dir)
dag = BuildDag()
dag.add(StreamUtilsStep(output_dir="include"))      # ArrayUtilsStep depends on this
dag.add(ArrayUtilsStep(Float32, [32, 64]))          # element type, supported word widths
dag.run(cfg)
```

**Artifact:**  The above code builds two files:
-  `include/float32_array_utils.h` containing the class definition and helpers to be used in synthesizable code
- `float32_array_utils_tb.h` for helpers that may not be synthesizable (like file I/O) that can the testbench. 

The first file declares the `float32_array_utils::` namespace with the geometry constants
(`pf<>` / `lane_capacity<>` / `get_nwords<>`), the element-range `read_array_slice`/`write_array_slice`, the
per-call lane methods `read_array_lane`/`write_array_lane`, and the stream variants.

The defining property of `raw` mode: **the generated code is keyed on the element type, not on a particular
array.** There is no per-array struct — the same `float32_array_utils` helpers serve *every* `raw` array of
`float` elements, of any length. (The [`struct`](./struct.md) mode is the opposite: it generates a
wrapper type per array.)

## Declaring vectors in C++

Once the header files are generated, any Vitis C++ function can write code using these helpers.
Th Vitis HLS equivalent of the `raw` array is simply a flat C array of the element's `value_type`. (On the Python side this is
`cpp_storage="raw"`, which requires `static=True` and a 1-D shape — see
[Data Arrays](../../schema/python/dataarrays.md).) In a kernel you declare it directly:

```cpp
#include "float32_array_utils.h"
namespace au = float32_array_utils;

static const int N = 256;
au::value_type x[N];     // == float x[256]
```

Using `au::value_type` rather than a hard-coded `float` keeps the declaration tied to the schema: change the
element type in Python, regenerate, and the C++ follows.

## Packing factors

As discussed in the section on [serialization](../../schema/hls/serialization.md),  arrays must be transferred over **channels**, such as AXI4-streams, or memory-mapped interfaces.  These channels may have **word bitwidth**, typically denoted `word_bw`, that may be larger or smaller than the bitwidth of each element of the array.  The generated include files provide methods to **serialize** and **deserialize** arrays of elements over channels of any width. 

Given a `word_bw`, two dual quantities describe how the elements line up with the words:

- the **packing factor** — `pf = ⌊word_bw / element_bits⌋` — how many whole elements fit in one word; and
- the **words per element** — `words_per_elem = ⌈element_bits / word_bw⌉` — how many words one element spans.

Exactly one of them exceeds 1 (both are 1 when `word_bw == element_bits`). A **wide channel**
(`word_bw ≥ element_bits`) packs `pf` elements per word — the vectorized case below — while a **narrow
channel** (`word_bw < element_bits`) spreads each element across `words_per_elem` words.

As an example, suppose a data structure is a 2D point:

```python
Float32 = FloatField.specialize(bitwidth=32)
class Point2D(DataList):
    elements = {
        "x":  {"schema": Float32},
        "y":  {"schema": Float32}
    }
```

This structure will have a `bitwidth = 64`.
After we generate the include file `point_2d.hpp`:  We can obtain the parameters:


```cpp
#include "point_2d_array_utils.h"
namespace point2d = point2d_array_utils;
static constexpr int PF      = point2d::pf<WORD_BW>();             // WORD_BW / 64 (0 if WORD_BW < 64)
static constexpr int LW      = point2d::lane_capacity<WORD_BW>();  // max(1, PF): lane-buffer size + loop step
static constexpr int elem_bw = point2d::value_bitwidth;           // 64
static constexpr int wpe     = point2d::get_nwords<WORD_BW>(1);    // ceil(64 / WORD_BW): words per element
```

Each `pf` element slot in a word is a **lane**. When `pf ≥ 1`, a loop that touches all `pf` lanes per cycle
does `pf` elements of work per cycle — so **`WORD_BW` is the throughput lever**. For the 64-bit `Point2D`:

| `WORD_BW` | `pf = ⌊WORD_BW/64⌋` | `words_per_elem = ⌈64/WORD_BW⌉` |
|---|---|---|
| 32  | 0 — element spans 2 words | 2 |
| 64  | 1 | 1 |
| 128 | 2 | 1 |
| 256 | 4 | 1 |
| 512 | 8 | 1 |

At `WORD_BW = 32` the element is wider than the word, so `pf` is 0 and each `Point2D` occupies two words;
from `WORD_BW = 64` up, whole elements fit per word. (For the schema-level view — `n_words` and the
per-transfer cycle cost — see [Serialization](../../schema/hls/serialization.md).)



## Reading and writing a range — `read_array_slice`

When you want the array (or a sub-range) resident — a coefficient table loaded once, a strided row — and
don't need cycle-level scheduling, `read_array_slice` moves an arbitrary element range `[i0, i1)` in one
call, working entirely in **element coordinates**:

```cpp
float x[N];
au::read_array_slice<WORD_BW>(x_words, x);            // whole array → x[0..N)   (static-size overload)
au::read_array_slice<WORD_BW>(x_words, i0, i1, x);    // elements [i0, i1)  → x[0 .. i1-i0)
au::write_array_slice<WORD_BW>(x, x_words, i0, i1);   // x → words [i0, i1)
```

It locates `i0`'s word for you and is **division-free** — a running bit offset plus a wrapping lane counter,
correct for *any* `pf` (including non-powers-of-two, where `i / pf` and `i % pf` would otherwise synthesize as
real hardware dividers). It handles partial-word ends and wide elements (`pf = 0`) internally, so the kernel
never writes `i0 / pf` or `i % pf`. An unaligned `write_array_slice` read-modify-writes the shared boundary
words rather than clobbering the neighbor elements packed beside the range.

The packed word count for `N` elements is `get_nwords<WORD_BW>(N) = ⌈N · element_bits / WORD_BW⌉` — the
array-utils helper that mirrors the schema-level `n_words`.

Use `read_array_slice` when you want the data resident; when you want **throughput**, drop to the lane loop
below.

## The lane loop

The **lane methods** move the next `LW = lane_capacity<WORD_BW>() = max(1, pf)` elements per call — `pf`
lanes of a word in the vectorized regime (`WORD_BW ≥ element_bits`), or one wide element spanning
`⌈element_bits / WORD_BW⌉` words when `pf = 0`. You step the loop by `LW`, read into a **partitioned** lane
buffer, `UNROLL` the compute across the lanes, and write back — **one shape that covers both regimes with no
branch**. Here the kernel computes `y[i] = x[i] * x[i]`:

```cpp
#include "float32_array_utils.h"
namespace au = float32_array_utils;
static const int PF  = au::pf<WORD_BW>();              // 0 if element wider than the word
static const int LW  = au::lane_capacity<WORD_BW>();   // = max(1, PF): elements per step and per buffer
const int WPU = au::get_nwords<WORD_BW>(LW);           // words advanced per step

const ap_uint<WORD_BW>* xp = x_words;                  // running pointers — no per-iteration divide
ap_uint<WORD_BW>*       yp = y_words;
for (int i = 0; i < N; i += LW) {
#pragma HLS PIPELINE II=1
    float lane[LW];
#pragma HLS ARRAY_PARTITION variable=lane complete dim=1     // lanes in parallel registers
    const int n = (N - i < LW) ? (N - i) : LW;               // tail: last (partial) word

    au::read_array_lane<WORD_BW>(xp, lane, n);               // LW elements in
    for (int k = 0; k < LW; ++k) {
#pragma HLS UNROLL
        if (k < n) lane[k] = lane[k] * lane[k];              // one element per lane
    }
    au::write_array_lane<WORD_BW>(lane, yp, n);              // LW elements out
    xp += WPU; yp += WPU;
}
```

The three pragmas are what vectorize the loop:

- **`ARRAY_PARTITION variable=lane complete`** — splits the lane buffer into `LW` registers so every lane is
  live in the same cycle (otherwise it is a BRAM and the `UNROLL` serializes on memory ports).
- **`UNROLL`** — instantiates `LW` parallel copies of the compute.
- **`PIPELINE II=1`** — issues one word (so `pf` elements) per cycle in the vectorized regime.

The **running pointers** `xp`/`yp` are a small but real win: advancing by a constant `WPU` words per iteration
gives the address directly, rather than `x_words + i / PF` — avoiding a per-iteration hardware divide.

The same loop covers the **wide-element regime for free**: when `pf = 0`, `LW = 1` and `WPU = ⌈element_bits /
WORD_BW⌉`, so each step pulls one element across `WPU` words and HLS relaxes `II` to `WPU` — one element per
`WPU` cycles, the honest cost of a wide element. There is no `pf = 0` special case, and the `n` argument is
simply ignored (`LW = 1`).

Widen `WORD_BW` and `PF` rises with it; the same loop retires more elements per cycle. That is the
bus-width → throughput relationship made concrete (and what the VMAC `mem_dwidth` sweep measures).

### Streams instead of memory

The same lane loop runs over a stream port — you drop the running pointers (the stream
self-sequences) and use the stream lane variants (`read_stream_lane` / `read_axi4_stream_lane`, the
latter carrying `TLAST`). Moving data over a real stream or `m_axi` port inside a kernel — the stream
lane calls and the worked `.tpp` examples — is covered in
[Custom Hooks: kernel patterns](../../custom_hooks/patterns.md).

## See also

- [Serialization](../../schema/hls/serialization.md) — the schema-level packing model and `word_bw`.
- [Data Arrays](../../schema/python/dataarrays.md) — declaring `DataArray`, `struct` vs `raw`.
- [`struct`](./struct.md) — the same packing behind a generated struct's methods.
- [`complex`](./complex.md) — complex elements (wireless).
