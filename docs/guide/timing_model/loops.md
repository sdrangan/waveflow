---
title: The form of a span's model
parent: Timing Models
nav_order: 5
audience: python
api: [LinearTiming, LinCalibModel, TimedPhase]
summary: "What a span's model is for the common case, a pipelined loop: m iterations cost latency + ii·(m − 1) cycles. A sequential span is that whole formula; a pipelined phase splits it into its two spans, latency (a constant) and fill_time (ii·(m − 1)). Both are linear in the features, which is what LinearTiming fits: an intercept plus a coefficient per feature, one LinCalibModel per span. Poly's fitted fill_time, -1 + 1·nwords, is ii·(m − 1) with ii = 1. Unrolling makes the trip count ceil(n / U): compute it in the body and pass it as the feature."
---

# The form of a span's model

A [timed phase](./timed_phases.md)'s model predicts each span from its features.  Its *form* -- what
shape that function has -- follows from what the hardware does, and the common case is a **loop**: one
iteration per sample, or per few.

## A pipelined loop costs `latency + ii·(m − 1)`

Take a length-`n` inner product -- the shape of most DSP kernels.  The synthesized kernel is a loop
with a pipeline pragma:

```cpp
float compute(const float x[N], const float coef[N]) {
    float y = 0;
    for (int i = 0; i < N; i++) {
        #pragma HLS pipeline II=1
        y += coef[i] * x[i];
    }
    return y;
}
```

A pipelined loop that issues `m` iterations takes

```
cycles = latency + ii · (m − 1)
```

- **`latency`** -- the **pipeline depth**: cycles from an input entering the pipeline to its result
  coming out.  It is the cost of the **first** iteration's result.
- **`ii`** -- the **initiation interval**: cycles between successive iterations entering the pipeline.
  `II = 1` starts one every cycle; `II = 2` every other.  It sets the **throughput**.

Reason it out from the two ends: the first result lands `latency` cycles in; after it the pipeline
emits one more every `ii` cycles, and `m − 1` are left, so they add `ii · (m − 1)`.  (`latency + ii · m`
is the common off-by-one -- it charges an `ii` for the first result, already paid inside `latency`.
With `m = 1` the formula correctly collapses to `latency`.)

## How it maps onto the spans

The two shapes of body use the formula differently:

| body | span | form |
|---|---|---|
| [sequential](./block.md) -- the loop runs after the whole input | `compute_time` (input last → output first) | `latency + ii·(m − 1)` -- the whole formula |
| [pipelined](./streaming.md) -- the loop runs while the input arrives | `latency` (input first → output first) | `latency` -- a constant |
| | `fill_time` (output first → output last) | `ii·(m − 1)` |

A pipelined phase is the same formula, split at the moment the first result comes out -- which is why
it needs two spans and a sequential one needs one.

## Linear in the features: `LinearTiming`

Every one of those forms is **linear** in the trip count: an intercept plus a coefficient times `m`.
That is the default model family, `LinearTiming`: for each span, one
[`LinCalibModel`](../calib/models.md) -- `intercept + Σ coefficient · feature` over the phase's
declared features.  The parameters of the hardware are read off the fitted coefficients:

- a pipelined phase's `latency` span fits a constant: the intercept **is** `latency`;
- its `fill_time` fits `−ii + ii·m`: the coefficient of `m` **is** `ii`;
- a sequential span fits `(latency − ii) + ii·m`.

Poly at 32 bits, [calibrated against its RTL](./fit.md), comes out as `latency = 34` and
`fill_time = −1 + 1·nwords`: a 34-cycle pipeline at `II = 1`.  None of those numbers is in the module's
code; the declaration only says which spans exist and what they depend on.

## Unrolling

Unrolling the loop by `U` processes `U` elements per iteration, so the iterations actually issued --
the **effective trip count** -- are `m = ceil(n / U)`, not `n`.  That is not a fitted coefficient; it
is a different feature.  Compute it in the body, where `U` is known, and pass it:

```python
m = math.ceil(n / self.unroll)
yield from self.loop_phase.pipeline(compute, load=self.s_in.load_array(T, n),
                                    store=self.m_out.store_array(T, n), trips=m)
```

with the phase declared over `features=("trips",)`.  At large `m` the throughput asymptote
`cycles / n → ii / U` is what pins `U` down if it is unknown.  The rule is general: a feature is
whatever the body knows before the span starts, in the form the hardware's cost is linear in.

## Another form

`TimedPhase(..., tm=...)` takes the family.  `LinearTiming` is the only one today; a span whose
duration is not linear in anything the body can compute -- a saturating curve, a lookup -- needs a
family of its own (the calibration layer already has an interpolating model,
[`InterpCalibModel`](../calib/models.md), to build one on).

## See also

- [Sequential bodies](./block.md) / [Pipelined bodies](./streaming.md) -- the two shapes.
- [Calibrating a timed phase](./fit.md) -- recovering `latency` and `ii` from an RTL trace.
- [Models](../calib/models.md) -- `LinCalibModel` and the calibration layer underneath.
