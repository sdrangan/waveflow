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

This page is the entry point: the selling point, the **two ways to compute** on
array data, when to use each, and an honest framing of the tradeoff. The section
splits in two:

**[Python](./python/)** — the NumPy-backed value model, per element kind:

- [Integer vectorization](./python/integer.md) — NumPy integer arrays, growth-aware
  operators, the width-tracking caveat.
- [Float vectorization](./python/float.md) — NumPy passthrough and golden references.
- [Fixed-point vectorization](./python/fixed.md) — full-precision `a*b + c`, one explicit
  `quantize`, bit-exact with `ap_fixed`. (The fixed-point *type* itself is on the
  [FixedField](../schema/python/fixpoint.md) page.)
- [Complex vectorization](./python/complex.md) — complex arithmetic and the
  numpy-vs-hardware multiply edge.

**[HLS](./hls/)** — the generated array helpers in a synthesizable Vitis kernel (the
schema-level packing model is in [Serialization](../schema/hls/serialization.md)):

- [Vitis: raw arrays](./hls/raw.md) — the flat array, packing factor, lanes, and the
  throughput lane loop.
- [Vitis: struct arrays](./hls/struct.md) — the generated wrapper struct's whole-array
  methods.
- [Vitis: complex arrays](./hls/complex.md) — complex elements end-to-end (the wireless
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

## The two paths

There are two ways to compute on array data, and the distinction matters most for
fixed-point:

### 1. The NumPy escape hatch — `.val`

`DataArray.val` is the raw underlying `ndarray`. Reach through it and you get plain
NumPy: maximum speed, every NumPy function available, and **you** manage the result
width and dtype.

```python
import numpy as np
from waveflow.hw.dataschema import DataArray, FloatField

F32 = FloatField.specialize(32)
a = DataArray.specialize(F32, max_shape=(3,))(np.array([1.5, 2.5, -3.0], np.float32))
b = DataArray.specialize(F32, max_shape=(3,))(np.array([2.0, -1.5, 0.5], np.float32))

y = a.val * b.val + 0.25          # raw numpy float32 — you own the dtype
y                                  # array([ 3.25, -3.5 , -1.25], dtype=float32)
```

### 2. Type-preserving operators — `a*b + c`, then `quantize`

The operators (`+`, `-`, `*`) on a `DataArray` are **type-preserving**: they read
the operands' formats, run the vectorized NumPy op underneath, and **derive the
result format** with full precision — no silent loss. They are sugar over the
underlying `mult`/`add`/`sub` functions. Rounding back to a working format is an
**explicit** `quantize(x, fmt)` — exactly mirroring `ap_fixed<...> y = a*b + c;` in
HLS, where the product and sum grow to full width and the assignment is the one
lossy step.

```python
from waveflow.hw.fixpoint import FixedField, from_real, quantize, to_real

Q = FixedField.specialize(8, 4)                  # ap_fixed<8, 4>
a = from_real([1.5, -2.0, 0.5], Q)
b = from_real([2.0,  1.5, -1.0], Q)
c = from_real([0.5,  0.25, -0.5], Q)

full = a * b + c                                  # ap_fixed<17, 9> — full precision, no loss
y    = quantize(full, Q)                          # ap_fixed<8, 4>  — the one explicit rounding
to_real(y)                                        # array([ 3.5 , -2.75, -1.  ])
```

### When to use which

| Path | Use it when | Cost you own |
|------|-------------|--------------|
| `.val` (numpy escape) | **float** math; or you genuinely want raw NumPy and will manage widths yourself | result dtype/width is on you |
| operators + `quantize` | **fixed-point** (and growth-aware **integer**) math you want bit-exact with HLS | nothing — formats are derived, loss is explicit |

The short rule:

- **Float** — `.val` is fine. IEEE-754 `float32`/`float64` don't grow, so raw NumPy
  *is* the bit-exact model; the operators are just NumPy passthrough over the same
  arrays.
- **Fixed-point** — use the **operators**. Fixed-point arithmetic grows bits
  (`a*b` is `Wa+Wb` wide) and rounds on assignment; the operators track that growth
  and keep the one rounding explicit, so your Python matches `ap_fixed` exactly.
  Doing fixed-point by hand through `.val` means re-deriving formats and rounding
  yourself — easy to get subtly wrong.
- **Integer** — either works; the operators add growth-aware width tracking
  (`a*b` → `Wa+Wb` bits, `a+b` → `+1`) with a fail-fast guard above 64 bits, which
  raw `.val` NumPy won't give you (it silently wraps). See
  [Integer vectorization](./python/integer.md).

Both paths keep data in NumPy arrays the whole way — that's what makes the
simulation fast. The operators just add the bit-growth bookkeeping on top.

## See also

- [FixedField](../schema/python/fixpoint.md) — the fixed-point *type* (the `ap_fixed`
  model, `QMode`/`OMode`, defaults-match-Vitis).
- [`examples/basic_vec`](../../examples/basic_vec/) — the worked front-door: one MAC,
  `y = a*b + c`, computed with these operators and checked **bit-exact against
  Vitis** for int / float / fixed.
- [Timing Analysis](../timing/) — where cycle/throughput modeling lives (the
  separate, non-functional concern).
