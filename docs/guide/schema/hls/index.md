---
title: HLS
parent: Data Schemas
nav_order: 2
has_children: true
audience: hls
summary: "The synthesizable C++ a schema generates — the struct codegen, and single-schema serialization (pack/read/write one value over each interface)."
---

# Data Schemas — HLS

The **HLS** side is the synthesizable C++ Waveflow generates from a Python schema, bit-for-bit
compatible with the Python model. These pages cover [code generation](./codegen.md) — the C++ struct
each schema becomes — [serialization](./serialization.md) — moving a **single** schema value in and
out over a packed integer, an `m_axi` memory port, a FIFO, or an AXI4-Stream — and
[test-data files](./tbutils.md), the `uint32` binary bridge that carries vectors between the Python
golden and the Vitis testbench.

For packing **arrays** of schemas — the packing factor, lanes, and the vectorized lane loop — see
[Vectorization](../../vectorization/).
