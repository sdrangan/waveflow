---
title: Basic vector arithmetic
parent: Examples
nav_order: 1
has_children: true
summary: "The front door for vectorization, and the smallest demonstration of the claim the rest of the framework rests on: one element-wise multiply-accumulate, computed over NumPy arrays with no per-element Python loop, in integer, float and fixed point — and asserted equal to the Vitis kernel bit for bit in all three. A data and schema example, so it comes before any module-to-module interface exists."
---
# Basic Vectorization — one MAC, bit-exact

`basic_vec` is the **front-door for [vectorization](../../guide/vectorization/)**: the
smallest demonstration that a Python-vectorized golden model and a vectorized Vitis kernel
produce **the same bits**. It is a *data/schema* example — how to **represent and compute on
data**, before any module-to-module interface is introduced.

The whole example is one elementwise multiply-accumulate, computed over arrays (no
per-element Python loop) for each of the three numeric kinds, with the result asserted equal
to Vitis C-sim **bit-for-bit**:

```python
y = a * b + c
```

| kind | Python | Vitis kernel | bit-exact because |
|------|--------|--------------|-------------------|
| **integer** | growth-aware operators (`Int17` result) | `ap_int<17> y = a*b + c;` | integer arithmetic is exact; the operators track the growth |
| **float** | numpy `float32` passthrough | `a*b + c`, built `-ffp-contract=off` | same two roundings (no fused FMA) |
| **fixed** | `quantize(a*b + c, Q)` | `ap_fixed<8,4> y = a*b + c;` | full precision, then quantize-on-assign |

This is the **teaching** counterpart to the rigorous all-modes/all-widths sweep in
`examples/schemas/fixedpoint` — the two share the same conformance machinery (`BuildDag` +
`run_dag_cli` + gen→csim→compare-bits). The *concepts* (operators, the two paths, growth
rules) live in the [vectorization guide](../../guide/vectorization/); this walkthrough shows
them end-to-end on one example.

## Learning Objectives

In going through this example, you will learn to:

- Represent numbers as typed, **vectorized** `DataSchema` arrays (numpy-backed, no
  per-element Python loop) across the three numeric kinds — integer, float, and
  fixed-point.
- Apply the type-preserving operators (`a*b + c`) and let the **result type follow the
  growth rules**, so the Python golden is bit-exact by construction.
- Hand-write the three matching minimal Vitis kernels and understand *why* each is
  bit-exact — integer exactness plus growth, float built `-ffp-contract=off` (no fused
  FMA), fixed-point quantize-on-assign.
- Drive a **gen→csim→compare-bits** conformance DAG (`BuildDag` + `run_dag_cli`) and
  assert the Vitis output equals the Python operator output **bit-for-bit**.
- See how this front-door relates to the exhaustive all-modes/all-widths sweep in
  `schemas/fixedpoint`, which shares the same conformance machinery.

## The walkthrough

1. **[The Python model](./python.md)** — the vectorized golden: declare the arrays, apply
   `a*b + c`, derive the result type, emit the golden bits.
2. **[The Vitis equivalent](./vitis.md)** — the hand-written C++ kernels that mirror the op.
3. **[Confirming the match](./eval.md)** — the build DAG, the Vitis C-sim, the bit comparison.

## File map

In [`examples/basic_vec/`](../../../examples/basic_vec/):
- `basic_vec_build.py` — the three MAC cases + the gen→csim→compare conformance DAG.
- `kernels.py` — the three minimal, **hand-written** Vitis kernels (int / float / fixed).
- `run.tcl` — the Vitis C-sim driver (`-ffp-contract=off` for the float kernel).

## Running it

```bash
python examples/basic_vec/basic_vec_build.py --through gen   # kernels + vectors + golden (no Vitis)
python examples/basic_vec/basic_vec_build.py --through run   # the bit-exact csim conformance (Vitis)
```

The `run` stage asserts, per kind, that the Vitis output bits equal the Python operator bits
**exactly**; any mismatch stops the build.
