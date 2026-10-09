---
title: Python
parent: Vectorization
nav_order: 1
has_children: true
audience: python
summary: "The NumPy-backed vectorized value model — integer, float, fixed-point, and complex arrays — for loop-free Python computation that stays bit-exact with the generated hardware."
---

# Vectorization — Python model

The **Python model** is how Waveflow holds and computes array values: NumPy-backed, loop-free, and
bit-exact with the hardware it generates. These pages cover the per-element-type numerics — the
[integer](./integer.md), [float](./float.md), [fixed-point](./fixed.md), and [complex](./complex.md)
vectorized models, including the two paths (the `.val` NumPy escape hatch vs. the type-preserving
operators) and the result-format rules.

Values arrive already typed. A `DataArray`'s NumPy values are `.val`, so don't wrap them in
`np.asarray(...)`. A field read is a NumPy scalar that works as an index, a count, a shape or a
compare, so don't wrap it in `int(...)`. Convert only where a value leaves hardware semantics for
plain Python: signed arithmetic, JSON, text output. See
[What a field read returns](../../schema/python/datalists.md#what-a-field-read-returns) and
[Reading the values](../../schema/python/dataarrays.md#3-reading-the-values-val).

For the synthesizable side — packing these arrays into Vitis C++ words, the lane loop, and the
storage modes — see [HLS](../hls/).
