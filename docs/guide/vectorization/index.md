---
title: Vectorization
parent: Guide
nav_order: 3
has_children: true
---

# Vectorization

Vectorization is how Waveflow's functional simulation is **fast *and* bit-exact**.
Data lives in NumPy arrays from end to end — operands, intermediates, and results
are all `ndarray`s — so a whole vector of values flows through one C-level NumPy
call instead of a Python loop over elements. The numbers Waveflow computes match
the Vitis HLS datapath **bit-for-bit**, and they are computed at NumPy speed.

## Outline

We describe how to implement arrays in two sections:

**[Python](./python/)** — defining NumPy-backed vectors in the Python model and computing on them:

- [Numerical operations](./python/numerical.md) — the shared model for **every** element type:
  defining vectors, the `.val` NumPy escape hatch, and the type-preserving operators (`a*b + c`,
  then `quantize`).
- [Integer vectorization](./python/integer.md) — NumPy integer arrays, growth-aware
  operators, the width-tracking caveat.
- [Float vectorization](./python/float.md) — NumPy passthrough and golden references.
- [Fixed-point vectorization](./python/fixed.md) — full-precision `a*b + c`, one explicit
  `quantize`, bit-exact with `ap_fixed`. (The fixed-point *type* itself is on the
  [FixedField](../schema/python/fixpoint.md) page.)
- [Complex vectorization](./python/complex.md) — complex arithmetic and the
  numpy-vs-hardware multiply edge.

**[HLS](./hls/)** — Vectors can also be used in the generated Vitis HLS code using the synthesizable Vitis kernel (the
schema-level packing model is in [Serialization](../schema/hls/serialization.md)).  These are described in three parts:

- [Raw arrays](./hls/raw.md) — the flat array, packing factor, lanes, and the
  throughput lane loop.
- [Struct arrays](./hls/struct.md) — the generated wrapper struct's whole-array
  methods.
- [Complex arrays](./hls/complex.md) — complex elements end-to-end (the wireless
  vertical).

## Why vectorization is the differentiator

The Waveflow thesis is bit-exact *and* fast. The speed comes from keeping data
**vectorized**: a `DataArray` is numpy-backed, and `.val` is the underlying
`ndarray`. `FixedField` is deliberately **integer-backed on a single 64-bit dtype**
(not an arbitrary-precision object array) **specifically so fixed-point arrays stay
vectorized** — every fixed-point op is a NumPy integer op over the whole array.

This is a deliberate **abstraction/speed tradeoff**, not a claim that other tools
are wrong:

- **Per-element fixed-point packages** (arbitrary-precision Python fixed-point
  libraries) model each value exactly at any width, but fall back to per-element
  Python for big widths — correct, but not vectorized, so slower over large arrays.
- **RTL / cycle-level Python simulators** (e.g. PyMTL) model the design
  cycle-by-cycle. That is a *different abstraction level*: they pay per-cycle costs
  that don't vectorize over data, in exchange for cycle-accurate timing.

Waveflow sits at the **transaction level with vectorized data**: it gives fast
**functional** (bit-exact) simulation, and handles timing
[separately](../timing/). Pick the level that fits the question you're asking — for
"are my bits right, fast, over a lot of data," vectorized functional sim is the
sweet spot.

## See also

- [Numerical operations](./python/numerical.md) — the two compute paths (`.val` vs the
  type-preserving operators) in depth.
- [FixedField](../schema/python/fixpoint.md) — the fixed-point *type* (the `ap_fixed`
  model, `QMode`/`OMode`, defaults-match-Vitis).
- [`examples/basic_vec`](../../examples/basic_vec/) — the worked front-door: one MAC,
  `y = a*b + c`, computed with these operators and checked **bit-exact against
  Vitis** for int / float / fixed.
- [Timing Analysis](../timing/) — where cycle/throughput modeling lives (the
  separate, non-functional concern).
