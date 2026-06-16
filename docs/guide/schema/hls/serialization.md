---
title: Serialization
parent: HLS
grand_parent: Data Schemas
nav_order: 2
audience: hls
api: [serialize, deserialize, pack_to_uint, unpack_from_uint, read_array, write_array, read_stream, write_stream, read_axi4_stream, write_axi4_stream]
summary: "Move a single schema value to and from fixed-width words — the C++ per-interface methods (pack_to_uint / read_array / read_stream / read_axi4_stream) and the Python serialize / deserialize to Words — generated from one definition so they agree bit-for-bit. Covers word_bw, nwords, the Words dtype rule, and the 8192-bit packed-integer cap."
---

# Serialization & Deserialization

## What is serialization and deserialization

**Serialization** flattens a structured value — a schema, with its fields and bit widths — into a plain
sequence of fixed-width **words**; **deserialization** reverses it. Hardware needs this because the things
that *carry* data — an `m_axi` memory region, a FIFO, an AXI-Stream (see [Interfaces](../../interface/)) —
move raw words, not structured types: to store a value in memory or send it between modules you first pack
it into words, and unpack it at the other end.

A value of `B` bits (`B = <Schema>::bitwidth`) packs **LSB-first, with no padding**, into words of some
channel width `word_bw`, occupying `n_words = ⌈B / word_bw⌉` of them. The layout is independent of
`word_bw` — widening the channel only moves where the word *boundaries* fall.

In Waveflow you never hand-write this packing. **Both** the Python `DataSchema` class **and** its generated
Vitis HLS C++ struct carry built-in serialize/deserialize methods, generated from the *same* schema
definition — so for a given `word_bw` they produce **identical words, bit-for-bit**. That equivalence is
what lets a Python-generated test vector drive a Vitis kernel, and a kernel's output be checked against the
Python golden.

## Serialization and deserialization in Vitis HLS

In a kernel, serialization targets an array of `ap_uint` words:

```c
ap_uint<word_bw> words[nwords];   // nwords = <Schema>::nwords<word_bw>()
```

The generated struct has **one read/write pair per interface**, each templated on the channel width `W`
(`= word_bw`):

| Interface | Read | Write |
|---|---|---|
| Packed integer | `unpack_from_uint(u)` | `pack_to_uint()` |
| Memory (`m_axi`) | `read_array<W>(words)` | `write_array<W>(words)` |
| FIFO stream | `read_stream<W>(s)` | `write_stream<W>(s)` |
| AXI4-Stream | `read_axi4_stream<W>(s, tl)` | `write_axi4_stream<W>(s, /*tlast=*/...)` |

```cpp
#include "include/poly_cmd_hdr.h"
PolyCmdHdr hdr; hdr.tx_id = 42; hdr.nsamp = 1024;
hdr.write_axi4_stream<32>(out_stream, /*tlast=*/false);   // serialize onto a stream

PolyCmdHdr rx; streamutils::tlast_status tl;
rx.read_axi4_stream<32>(in_stream, tl);                   // rx == hdr, bit-for-bit
```

- **Argument types.** `words` is an `ap_uint<W> words[nwords]` array; `s` is the channel's `hls::stream`.
- **Word count.** Size `words` with **`<Schema>::nwords<W>()`** (`= ⌈B/W⌉`).
- **Packed-integer limit.** `pack_to_uint` / `unpack_from_uint` move the whole schema as one **`ap_uint<B>`**.
  Vitis HLS caps `ap_uint` at **8192 bits**, so for `B > 8192` the packed form is unavailable — use the
  memory or stream methods instead.

Switching the channel width is a one-constant change to `W`; the packing rule is invariant.

## Serialization and deserialization in Python

On the Python side, a `DataSchema` value serializes to **`Words`** — a NumPy array of unsigned-integer words
— through the same packing rule:

```python
hdr = PolyCmdHdr(); hdr.tx_id = 42; hdr.nsamp = 1024
words = hdr.serialize(word_bw=32)                    # -> np.uint32 array
rx    = PolyCmdHdr().deserialize(words, word_bw=32)  # rx == hdr
```

- `inst.serialize(word_bw=32) -> Words` — pack the value into words of width `word_bw`.
- `Schema().deserialize(words, word_bw=32) -> Schema` — unpack them back.

The word dtype follows `word_bw`, so a word always fits its element:

| `word_bw` | `Words` representation |
|---|---|
| `≤ 32` | 1-D `np.uint32` array |
| `≤ 64` | 1-D `np.uint64` array |
| `> 64` | 2-D `np.uint64` array, shape `(n_words, ⌈word_bw/64⌉)` — each word split into 64-bit chunks, least-significant chunk first |

For the **same `word_bw`**, `serialize` produces exactly the words the C++ `write_array<word_bw>` writes —
the bit-for-bit agreement noted above. (Writing those words to disk to drive a Vitis testbench is
[Test Data Files](./tbutils.md) — just `serialize(word_bw=32)` to a little-endian binary.)

## Arrays of schemas

This page moves a **single** schema value. For an **array** — the packing factor `pf`, lanes,
`lane_capacity`, `read_array_slice`, and the vectorized lane loop — see
[Vectorization](../../vectorization/) (the raw-array page covers the lane loop in depth, with vs without
pipelining, and the wide-element `pf = 0` case).

## See also

- [Code Generation](./codegen.md) — the files and classes that get generated for a schema.
- [Test Data Files](./tbutils.md) — `serialize` / `deserialize` to the on-disk `uint32` exchange format.
- [Vitis: raw arrays](../../vectorization/hls/raw.md) — packing **arrays** of schemas: the lane loop,
  `read_array_lane` / `read_array_slice`, and wide-element (`pf = 0`) handling.
