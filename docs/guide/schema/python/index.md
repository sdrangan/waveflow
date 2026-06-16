---
title: Python
parent: Data Schemas
nav_order: 1
has_children: true
audience: python
summary: "The Python data-schema model — fields, lists, arrays, unions, and the fixed-point / complex element types — the single source of truth for layout and values."
---

# Data Schemas — Python model

The **Python model** is the single source of truth for every Waveflow data structure: its fields and
bitwidths, its in-memory (NumPy-backed) values, and the packing rule that the generated C++ mirrors
bit-for-bit. These pages cover building schemas in Python — the scalar [fields](./fields.md), the
composite [lists](./datalists.md) and [arrays](./dataarrays.md), the tagged [unions](./dataunion.md),
and the [fixed-point](./fixpoint.md) and [complex](./complex.md) element types used by signal-processing
kernels.

For the synthesizable side — the C++ a schema generates and how a single value moves over each
interface — see [HLS](../hls/).
