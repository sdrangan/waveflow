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

Moving an array over a real **stream or `m_axi` port** — the stream/`TLAST` variants and the
pipelined loop patterns over a port — is detailed in [Interfaces](../../interface/).
