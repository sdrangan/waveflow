---
title: Kernel transfer reference
parent: Custom Hooks
nav_order: 7
audience: hls
api: [read_array_lane, write_array_lane, read_array_slice, write_array_slice, read_stream_lane, write_stream_lane, read_axi4_stream_lane, write_axi4_stream_lane]
summary: "The in-kernel transfer-call cheat sheet: the m_axi memory calls (read_array_slice for a resident range, the read_array_lane lane loop for throughput) and the stream calls (read_stream_lane / read_axi4_stream_lane with TLAST), the common loop shapes, and the Python-to-C++ mapping table the pattern pages draw on."
---

# Kernel transfer reference

A hook moves typed data between a **port** and **lane buffers**, then runs the datapath. This page is
the lookup for those in-kernel calls — the [block](./block.md), [stream](./stream.md), and
[complex](./complex.md) pages each introduce the ones they need; this collects them in one place. The
element-keyed methods come from the generated `<element>_array_utils::` namespace (aliased `au`
below); the geometry (`pf`, `lane_capacity`, `get_nwords`) and the lane loop in depth are in
[raw arrays](../vectorization/hls/raw.md), and the single-schema model is in
[Serialization](../schema/hls/serialization.md).

## Over a memory (`m_axi`) port

The port is an `ap_uint<WORD_BW>*`. Two access shapes:

**A resident range — `read_array_slice` / `write_array_slice`** (the [block](./block.md) shape). Move
an arbitrary element range `[i0, i1)` in element coordinates (the kernel never computes `i0/PF`):

```cpp
au::read_array_slice<WORD_BW>(mem, x);             // whole array → x[0..N)  (static-size overload)
au::read_array_slice<WORD_BW>(mem, i0, i1, x);     // elements [i0, i1) → x[0 .. i1-i0)
au::write_array_slice<WORD_BW>(x, mem, i0, i1);    // x → words [i0, i1)  (unaligned ends RMW)
```

**The lane loop — `read_array_lane` / `write_array_lane`** (the [complex](./complex.md) shape). For
throughput: each call moves the next `LW = lane_capacity<WORD_BW>()` elements at a running word
pointer (you advance it by `get_nwords<WORD_BW>(LW)` words):

```cpp
au::read_array_lane<WORD_BW>(src, lane, n);        // LW elements in, n valid
au::write_array_lane<WORD_BW>(lane, dst, n);       // LW elements out
```

## Over a stream port

For an AXI-Stream the shape is the [stream](./stream.md) lane loop, but you **drop the running
pointers** (the stream sequences itself) and use the stream lane variants, which also carry `TLAST`:

```cpp
streamutils::tlast_status tl;
au::read_axi4_stream_lane<WORD_BW>(s_in,  lane, n, tl);             // LW elements off the stream
au::write_axi4_stream_lane<WORD_BW>(s_out, lane, /*tlast=*/last, n);
```

A plain FIFO (no `TLAST`) uses `read_stream_lane<WORD_BW>(s, dst, n)` /
`write_stream_lane<WORD_BW>(src, s, n)`.

## The common loop shapes

- **Load-to-array** ([block](./block.md)) — pull a whole operand resident, then compute at leisure:
  `read_array_slice<WORD_BW>(mem, x)` into a local array. Use when you need random access (a
  coefficient table, a strided row) and don't need cycle-level scheduling.
- **Lane loop (throughput)** ([complex](./complex.md)) — step by `LW`, read a lane buffer, `UNROLL`
  the compute across lanes, write back; advance the word pointer by `WPU`. One shape covers both
  regimes (vectorized and wide-element `pf = 0`). The full annotated loop with its three pragmas
  (`ARRAY_PARTITION complete` / `UNROLL` / `PIPELINE II=1`) is in
  [raw arrays — the lane loop](../vectorization/hls/raw.md#the-lane-loop).
- **Pipelined stream loop** ([stream](./stream.md)) — the same lane loop over a stream port (drop the
  pointers, use the stream lane variants above); the stream self-sequences, so it streams in at one
  beat per `LW`.

## Mapping the Python transfer interfaces to the kernel

A hook is the C++ realization of a Python transfer interface. The
[`ArrayTransferIF`](../interface/derived/array_transfer.md) calls map to the generated array-utils methods:

| Python (transactional model) | C++ (in the hook) |
|---|---|
| `ArrayTransferIFMaster(element_type=Float32).write(elements)` | `float32_array_utils::write_axi4_stream_lane<W>(src, s, tlast, n)` (looped over the burst) |
| `ArrayTransferIFSlave(element_type=Float32).get(count=n)` | `float32_array_utils::read_axi4_stream_lane<W>(s, dst, n, tl)` (looped) |
| an array resident in memory | `float32_array_utils::read_array_slice<W>(mem, out)` |
| `StreamIFMaster.write(obj)` where `obj` is a `DataList` | `obj.write_stream<W>(s)` — from the header the schema generates |
| `StreamIFSlave.get(Schema)` | `Schema c; c.read_stream<W>(s);` — **one call, never `n` × `s.read()`** |
| `StreamIFSlave.get(Elem, count=n)` | `elem_array_utils::read_stream_lane<W>(s, dst, n)` |

**The plain-`StreamIF` schema rows are the ones people miss.** A command read as `n` separate
`s.read()` calls authors the field layout a second time, in the one place nothing checks it against
the generated header — see [streams](../interface/primitive/stream.md#the-four-ways-to-move-data).

`ArrayUtilsStep` generates these for any `DataSchema` element type, so a transfer parameterized on a
composite `DataList` gets analogous helpers.

> **API note:** earlier drafts mapped these to the bulk `write_array(stream, …)` /
> `read_array(stream, …)` names. Those were the *memory* bulk methods (`read_array<W>(words, dst,
> len)`) misapplied to streams; the current per-port API is the lane/slice family above. The old
> bulk `read_array` / `write_array` (and the per-element `*_elem` wrappers) have been **retired**
> from the generator — resident arrays use `read_array_slice` / `write_array_slice` (element
> coordinates) or the `read_array_lane` loop, and all live examples read in this one idiom.

(The component-method `self.m_mem.read_array(...)` / `write_array(...)` in `run_proc` are a different,
**auto-generated** path — they lower to `read_array_slice` / `write_array_slice` bursts, see
[block](./block.md) — not hand-written hook calls.)

## See also

- [Raw arrays](../vectorization/hls/raw.md) — the lane loop in depth (pragmas, `pf = 0`) and `read_array_slice`.
- [Serialization](../schema/hls/serialization.md) — the single-schema methods and the `word_bw` model.
- [Interfaces](../interface/) — the Python transactional model these kernel calls realize.
- [Block](./block.md) / [Stream](./stream.md) / [Complex](./complex.md) — the patterns these calls live inside.
