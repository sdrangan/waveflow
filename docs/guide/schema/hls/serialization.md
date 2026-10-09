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

A value packs **LSB-first** into words of some channel width `word_bw`, by the [word layout
rule](#the-word-layout-rule) below. A field never straddles two words, and an array starts and ends on a
word boundary, so a value of `B` bits (`B = <Schema>::bitwidth`) can take more than `⌈B / word_bw⌉`
words. The count for each width is `<Schema>::nwords<word_bw>()` in C++ and `nwords_per_inst(word_bw)`
in Python.

In Waveflow you never hand-write this packing. **Both** the Python `DataSchema` class **and** its generated
Vitis HLS C++ struct carry built-in serialize/deserialize methods, generated from the *same* schema
definition — so for a given `word_bw` they produce **identical words, bit-for-bit**. That equivalence is
what lets a Python-generated test vector drive a Vitis kernel, and a kernel's output be checked against the
Python golden.

## The word layout rule

Every path follows this rule at every `word_bw`: Python `serialize` / `deserialize`, `nwords`, and the
C++ `read_array` / `write_array`, `read_stream` / `write_stream` and `read_axi4_stream` /
`write_axi4_stream`. A host written in another language must follow it too. Fields are placed in
declaration order with a cursor that starts at bit 0 of word 0:

1. **A scalar field** (`IntField`, `FloatField`, `EnumField`, ...) of `b` bits goes at the cursor if
   it fits in what is left of the current word. If it does not fit, it starts at bit 0 of the next
   word: a field never straddles two words. A field wider than a word (`b > word_bw`) starts on a fresh
   word and takes `⌈b / word_bw⌉` whole words.
2. **An array field** (`DataArray`) starts on a fresh word, unless the cursor is already at bit 0. With
   `pf = word_bw // elem_bw` elements to a word, element `k` goes in word `k // pf` of the array, at bit
   `(k % pf) * elem_bw`, so element 0 is in the low bits. A composite element (a `DataList`) is packed
   flat in its lane, in `pack_to_uint`'s layout. An element wider than a word (`pf = 0`) takes
   `elem.nwords<W>()` whole words of its own.
3. **The array closes its last word.** The field after an array starts on a fresh word, even when the
   array's last word has room left.
4. **A nested `DataList`** follows the same rules, as if its fields were inlined.

Rule 2 makes an array's words independent of what comes before it, so a kernel moves it a whole word at a
time with the `pf`-lane loop at II=1 (see [Vectorization](../../vectorization/)). The cost is up to one
partly-filled word on each side of the array.

For example, take a header with a 16-bit `tx_id`, four `float32` coefficients and a 16-bit `nsamp`:

```python
class PolyCmdHdr(DataList):
    elements = {
        "tx_id":  {"schema": TxId},        # 16 bits
        "coeffs": {"schema": CoeffArray},  # 4 x float32
        "nsamp":  {"schema": Nsamp},       # 16 bits
    }
```

| `word_bw` | words |
|---|---|
| 32 | `[tx_id]  [c0]  [c1]  [c2]  [c3]  [nsamp]` (6) |
| 64 | `[tx_id]  [c0 \| c1<<32]  [c2 \| c3<<32]  [nsamp]` (4, not 3) |

At 64 bits, `tx_id` and `c0` would fit in one word, but the array starts on a fresh word. The bits of a
word that no field uses are always zero.

`pack_to_uint` / `unpack_from_uint` use a different layout, the **flat** one: the fields concatenated
LSB-first with no word boundaries, `B` bits in all. It is a register image, not a word stream, so it does
not match `serialize` unless the schema has no arrays and fits in one word.

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
- **Word count.** Size `words` with **`<Schema>::nwords<W>()`**, from the [layout
  rule](#the-word-layout-rule). It can be more than `⌈B/W⌉`.
- **TLAST.** `write_axi4_stream<W>(s, true)` asserts TLAST on the message's last beat only, even when
  the message ends with an array.
- **Packed-integer limit.** `pack_to_uint` / `unpack_from_uint` move the whole schema as one **`ap_uint<B>`**,
  in the flat layout.
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

`deserialize` (and `read_array`) also accept a signed array, such as the `int64` TDATA samples from a VCD,
or a Python list of ints, signed or not. Each word is masked to its `word_bw`-bit pattern. A list is
converted one int at a time, never through NumPy's dtype inference: a list that mixes 64-bit words above
and below `2**63` would otherwise become `float64` and lose its low bits.

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
