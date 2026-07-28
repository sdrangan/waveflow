---
title: "Vitis: array serialization"
parent: HLS
grand_parent: Vectorization
nav_order: 4
audience: hls
api: [read_array, write_array, elem_read, elem_write, read_array_slice, write_array_slice, read_stream_lane, write_stream_lane, read_axi4_stream_lane, write_axi4_stream_lane, pack_to_uint, unpack_from_uint]
summary: "Move a whole ARRAY of a schema element to and from words — the generated <stem>_array_utils helpers (elem_read / read_array_slice / read_stream_lane / read_axi4_stream_lane) and the DataArray struct methods (read_array / pack_to_uint). The array analog of single-value Serialization: enumerates every channel you can (de)serialize an array to, the routine for each, and where it is generated."
---

# Array serialization & deserialization

[Serialization](../../schema/hls/serialization.md) moves a **single** schema value to and from words.
This page is its array analog: moving a **whole array** of a schema element — the thing a
[`DataArray`](./raw.md) lowers to — over the same channels (a memory buffer, a FIFO, an AXI4-Stream).
As with a single value, **you never hand-write the bit packing**: Waveflow generates the routines from the
element schema, and the Python model uses the identical layout, so a Python golden and a Vitis kernel agree
bit-for-bit.

If you find yourself writing `.range(32*l+31, 32*l)` in a kernel to pull elements out of a packed word,
**stop — there is a generated routine for it.** That the routine is easy to miss is a real problem (it is
generated per element type, into a file named after the element); this page is the "start here".

## Where the routines are generated

For an element schema `T`, `gen_array_utils(T)` emits **two** homes, and a `DataArray` of `T` emits a
third:

| File | Contains | Granularity |
|---|---|---|
| `<stem>_array_utils.h` (namespace `<stem>_array_utils`) | `elem_read` / `elem_write`, `read_array_slice` / `write_array_slice`, `read_array_lane`, and the stream lanes `read_stream_lane` / `read_axi4_stream_lane` (+ writes) | one element, one **lane** (`pf` elements), or a **range** — over a **runtime-length** buffer/stream |
| `array_utils.h` (namespace `array_utils`) | `read_array<T, W>(src, dst, n)` / `write_array<T, W>(...)` — generic over any element `T` | a whole runtime-length array ↔ a word buffer |
| `<snake>.h` (the `DataArray` struct, e.g. `UInt32Array` in `u_int32_array.h`) | `pack_to_uint` / `unpack_from_uint`, `read_array<W>` / `write_array<W>` | one **fixed-size** array value |

`<stem>` is the element name in snake case with `int`/`uint` normalized — e.g. element `IlElem` →
`il_elem_array_utils`; a `uint32` element → `uint32_array_utils`. `pf = W / 32` is the [packing
factor](./raw.md#packing-factors) (elements per word, called `LW` in some kernels).

## The channels — one routine per target

The packing rule (LSB-first, elements packed into each word from bit 0 up) is invariant; only the
**channel** changes. Pick by where the array lives:

| Channel | Read (→ `value_type[]`) | Write (← `value_type[]`) |
|---|---|---|
| **Memory buffer** — `ap_uint<W> words[]` (an `m_axi` region) | `read_array<T,W>(words, out, n)` · `read_array_slice<W>(words, i0, i1, out)` · `elem_read<W>(words, i)` | `write_array` · `write_array_slice` · `elem_write` |
| **Plain FIFO** — `hls::stream<ap_uint<W>>` | `read_stream_lane<W>(s, out, n)` | `write_stream_lane<W>(src, s, n)` |
| **AXI4-Stream** — `hls::stream<streamutils::axi4s_word<W>>` (framed, real `TLAST`) | `read_axi4_stream_lane<W>(s, out, n, tl)` | `write_axi4_stream_lane<W>(src, s, tlast, n)` |
| **Internal framed** — `hls::stream<streamutils::framed_word<W>>` (`{data,last}`, no sidebands) | `read_framed_stream_lane<W>(s, out, n, tl)` | `write_framed_stream_lane<W>(src, s, tlast, n)` |
| **Whole packed value** — `ap_uint<bitwidth>` (needs `bitwidth ≤ 8192`) | `unpack_from_uint(u)` | `pack_to_uint()` |

- **Random access** (the reason to load into a block first) is `elem_read<W>(words, i)` — it reads element
  `i` from a word buffer, hiding `words[i/pf]` + the lane mux. `elem_write` is its dual. This is what a
  gather (`Y[i] = X[P[i]]`) uses.
- **Ranges** are `read_array_slice<W>(words, i0, i1, out)` — a burst-friendly `[i0, i1)` copy that keeps
  the middle a pure aligned transfer (see the [slice codegen notes](./raw.md)).
- **A whole fixed array** at once is the `DataArray` struct's `read_array<W>(words)` (or the generic
  `array_utils::read_array<T,W>`).

## Streaming a whole array — the lane loop

For a straight drain, the **bulk** call reads the whole array in one line —
`au::read_axi4_stream<WORD_BW>(s, dst, tl, len)` (and `read_stream` / `read_framed_stream` for the other
framings) reads `len` elements, looping the lane internally. Reach for the **explicit lane loop** below
when you want to **vectorize the compute** — a partitioned lane buffer processed with `UNROLL`, `pf`
elements per beat — rather than materialize the whole array first; it is the same
[lane loop](./raw.md#the-lane-loop) as over memory but with a stream source. Either way the routines live
in the generated `<stem>_array_utils` namespace — alias it and qualify the calls (`au::`), the same
convention as [raw arrays](./raw.md#the-lane-loop) (a bare `read_*_lane` would not resolve):

```cpp
#include "float32_array_utils.h"
namespace au = float32_array_utils;                   // the generated namespace
static const int LW = au::lane_capacity<WORD_BW>();   // = max(1, pf): elements per beat and per buffer

au::value_type buf[N];                                // == float buf[N]
streamutils::tlast_status tl;
for (int i = 0; i < N; i += LW) {
#pragma HLS PIPELINE II=1
    const int n = (N - i < LW) ? (N - i) : LW;        // tail: last (partial) beat
    au::read_axi4_stream_lane<WORD_BW>(s, &buf[i], n, tl);   // AXI4-Stream source
}
// buf[] is now a typed float array — index it directly, no .range().  Unlike the memory lane
// loop in raw.md there is no WPU word-pointer to advance — a stream self-sequences.
```

Write the same way with `au::write_axi4_stream_lane` (pass `tlast` on the final beat). **Swap the lane
call for the channel:** `au::read_stream_lane` (plain FIFO) or `au::read_framed_stream_lane` (internal
`framed_word` edge) — the loop shape is identical.

## The three framings — all three now covered

Waveflow has **three** stream framings (see [streamutils](../../comp_codegen/composite.md)); the array-utils
generate a lane routine for each:

| Framing | Type | Array routine |
|---|---|---|
| plain | `hls::stream<ap_uint<W>>` | `read_stream_lane` / `write_stream_lane` |
| AXI4-Stream | `axi4s_word<W>` = `ap_axis<W,0,0,0>` (carries `keep`/`strb`) | `read_axi4_stream_lane` / `write_axi4_stream_lane` |
| internal framed | `framed_word<W>` = `{ data, last }` (no sidebands) | `read_framed_stream_lane` / `write_framed_stream_lane` |

`framed_word` is the lightweight `{data, last}` beat used on **internal** composite edges (the in-band
descriptor framing). Its array routines are generated by **renaming the AXI4-Stream ones** —
`framed_word` is field-identical to `axi4s_word` (`.data` / `.last`), so the packing and `TLAST` logic
have one source of truth (the same trick the schema layer uses for its single-value
`read_framed_stream`). A consumer of an internal framed edge — e.g. an `il_load` filling a block — should
call `read_framed_stream_lane` (in the [lane loop](#streaming-a-whole-array--the-lane-loop)) rather than
hand-looping `streamutils::read_boundary_word<framed_word<W>, W>`.

## Deprecated names

The `read_axi4_stream_elem` / `read_stream_elem` **public wrappers** were retired in serialization phase
2b; the `*_impl` structs they delegated to remain, but callers should use the `*_lane` routines above (or
`read_array_slice` for a range). If you see `_elem` in older code, it is the previous generation.

## See also

- [Serialization](../../schema/hls/serialization.md) — the single-value analog (one schema ↔ words).
- [Vitis: raw arrays](./raw.md) — the packing factor, lanes, `read_array_slice`, and the lane loop.
- [Interfaces](../../interface/) — moving arrays over real ports and the pipelined port patterns.
