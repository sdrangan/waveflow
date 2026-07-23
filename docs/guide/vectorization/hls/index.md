---
title: HLS
parent: Vectorization
nav_order: 2
has_children: true
audience: hls
summary: "Vectorized arrays in synthesizable Vitis C++ — the packing factor and lanes, the canonical lane loop, read_array_slice, and the raw / struct / complex storage modes."
---

# Vectorization — HLS

The **HLS** side is how a `DataArray` lowers to synthesizable Vitis C++: the packing factor `pf` and
lanes, the canonical lane loop (`read_array_lane` / `read_array_slice`), and the storage modes.
These pages cover the three modes — [raw](./raw.md) (a flat C array, explicit per-cycle control),
[struct](./struct.md) (a generated wrapper type), and [complex](./complex.md) (complex elements) —
each over a *local* array.

Moving a whole array to and from **words** — over a memory buffer, a FIFO, or an AXI4-Stream — is
enumerated in [Array serialization](./arrayutils.md) (the array analog of single-value
[Serialization](../../schema/hls/serialization.md)): the routine for each channel, where it is generated,
and the `framed_word` gap. The pipelined loop patterns over a real **port** are in
[Interfaces](../../interface/).
