---
title: Serialization
parent: HLS
grand_parent: Data Schemas
nav_order: 2
audience: hls
api: [pack_to_uint, unpack_from_uint, read_array, write_array, read_stream, write_stream, read_axi4_stream, write_axi4_stream]
summary: "Move a single schema value over each interface — pack_to_uint / read_array / read_stream / read_axi4_stream — the channel/word-width model, the per-interface read/write table, and bitwidth / nwords / the 8192-bit packed-integer cap."
---

# Serialization & Deserialization

Every schema knows how to **serialize** itself — convert its value to and from a flat sequence of
fixed-width words — and the *same* packing rule is generated for Python (simulation, golden vectors) and for
C++ (the synthesizable kernel), so the two agree **bit-for-bit**.

This page is the **single-schema methods reference**: moving *one* schema value over each interface. For
packing an **array** of schemas — the packing factor, lanes, and the vectorized lane loop — see
[Vectorization](../../vectorization/).

## The channel and the word width

Data moves over a **channel** — an AXI-Stream, an `m_axi` memory port, a FIFO — of some fixed width
`word_bw`. Bits pack **LSB-first, no padding**, so the layout is independent of `word_bw` — only where the
word *boundaries* fall changes. A value of `B` bits occupies `n_words = ⌈B / word_bw⌉` words.

Switching the channel width is a one-constant change to `word_bw`; the packing rule (and agreement with the
Python golden) is invariant.

## A single schema — across interfaces

The generated struct has **one read/write pair per interface**, each templated on the channel width `W`
(`= word_bw`). The schema's total bit width is the compile-time constant **`<Schema>::bitwidth`** (`B`), and
a value spans `⌈B/W⌉` words.

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

> For an **array** of schemas — the packing factor `pf`, lanes, `lane_capacity`, `read_array_slice`, and the
> canonical lane loop — see [Vectorization](../../vectorization/) (the raw-array page covers the lane loop in
> depth, with vs without pipelining, and the wide-element `pf = 0` case).

## See also

- [Code Generation](./codegen.md) — the files and classes that get generated for a schema.
- [Vitis: raw arrays](../../vectorization/vitis_raw.md) — packing **arrays** of schemas: the lane loop,
  `read_array_lane` / `read_array_slice`, and wide-element (`pf = 0`) handling.
