---
title: What we're building
parent: Rowwise FIR
nav_order: 1
has_children: false
audience: python
summary: "The computation: a real-valued FIR filter applied independently to every row of a matrix, with a 'valid' edge. The sliding T-tap window is what forces the input to be resident and randomly addressable — the reason this is a load-compute-store dataflow rather than a pure stream."
---

# What we're building

A **finite impulse response (FIR) filter**, applied independently to **every row** of an input matrix.
For an input row `X[i, :]` of length `n_col` and `T` filter taps `h`:

```
Y[i, j] = Σ_{t=0}^{T-1}  h[t] · X[i, j + (T-1) - t],     j = 0 … n_col − T
```

Each output is a weighted sum of the last `T` inputs — a sliding window of width `T`. We use the
**`valid`** edge: only outputs where the whole window lands inside the row are produced, so each row
yields `n_col − T + 1` outputs (no zero-padding, no edge cases). The example fixes `T = 8`.

## The golden

The numerical truth is one small numpy function,
[`fir_golden.py`](../../../examples/rowwise_fir/fir_golden.py) — vectorized over the matrix, with the
tap accumulation ordered **left-to-right in `t`** so it matches the hardware datapath *bit-for-bit*
(the C and RTL are float, and the order of float adds matters). Everything downstream — the simulation,
the C kernel, the RTL — is checked against this one function.

## The command

A host drives the accelerator through a memory command queue (the [`mmqueue`](../mmqueue/) interface).
Each `FIRCmd` ([`fir.py`](../../../examples/rowwise_fir/fir.py)) carries the taps `h`, the element
offsets of `X`, `h`, and `Y` in shared memory, and the matrix shape `n_rows` / `n_cols`. Addresses are
**element coordinates** (indices into the typed region), not bytes.

## Why FIR forces a resident buffer

FIR is the *minimal* computation that makes the load-compute-store structure a **necessity** rather
than a choice. The output `Y[i, j]` needs inputs `X[i, j−t]` for `t = 0 … T−1` — a **window** that
moves and overlaps. You cannot compute it from a pure stream of beats, because producing each output
requires *random* access back into the row. So the row must be **resident and randomly addressable**:
load the whole row into an on-chip buffer, compute the windowed sum over it, store the result.

That is the load-compute-store dataflow — and because it's *forced*, it is the cleanest possible
vehicle for teaching the pattern. The [next page](./dataflow.md) walks it.
