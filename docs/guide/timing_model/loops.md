---
title: Timing models for loops
parent: Timing Models
nav_order: 3
audience: python
api: [LinCalibModel]
summary: "The typical custom-hook compute is a loop, and a pipelined loop of m iterations costs latency + ii·(m − 1) cycles — latency is the pipeline depth (the first result), ii the initiation interval (each result after). That is linear in two parameters (latency, ii), so it is a LinCalibModel with a two-term basis map [1, m − 1] and coeff_names ['latency', 'ii'], fit_intercept=False. Unrolling by U reshapes the trip count to m = ceil(n / U). The parameters are set by hand now and fit from RTL later."
---

# Timing models for loops

The compute you describe for a [custom hook](../custom_hooks/) is very often a **loop** over the input
— one iteration per sample (or per few samples). This page is the timing model for that case: what a
pipelined loop costs, and how to express it as a fittable model.

## A loop, in Python and HLS

Take a length-`n` inner product — the shape of most DSP kernels:

```python
def compute(x, coef):          # the functional golden
    y = 0.0
    for i in range(n):
        y += coef[i] * x[i]
    return y
```

The synthesized kernel is the same loop with a pipeline pragma:

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

In pysim the Python `compute` runs instantly; the **timing model** says how long the *hardware* loop
would take.

## The closed form `latency + ii·(m − 1)`

A pipelined loop that issues `m` iterations takes:

```
cycles = latency + ii · (m − 1)
```

Two numbers describe the pipeline:

- **`latency`** — the **pipeline depth**: cycles from an input entering the pipeline to its result
  coming out. It is the cost of the **first** iteration's result.
- **`ii`** — the **initiation interval**: cycles between successive iterations entering the pipeline.
  An `II = 1` loop starts a new iteration every cycle; `II = 2` every other cycle. It sets the
  **throughput**.

Reason the form out from the two endpoints: the first result lands `latency` cycles in; after it, the
pipeline emits one more result every `ii` cycles, and there are `m − 1` results left, so they add
`ii · (m − 1)`. (Writing `latency + ii · m` is the common off-by-one — it charges an `ii` for the first
result, already paid inside `latency`. With `m = 1` the formula correctly collapses to `latency`.)

## It is linear in two parameters — a `LinCalibModel`

`latency + ii · (m − 1)` is **linear in the parameters** `latency` and `ii`: it is
`latency · 1 + ii · (m − 1)`, a two-term basis `[1, m − 1]` with those two as the coefficients. So it is
exactly a [`LinCalibModel`](../calib/models.md) — no new machinery:

```python
import math
from waveflow.calib.calib import LinCalibModel

self.tm = LinCalibModel(
    basis=["const", "trip"], target="cycles",
    coeff_names=["latency", "ii"], fit_intercept=False,   # the coeffs ARE the parameters
    transform=lambda r: [1.0, r["n"] - 1],                # the basis map: [1, m − 1], m = n here
    seed={"latency": 8, "ii": 1},                         # hand values now; fit later
)
self.tm.load_or_default()

cycles = self.tm.predict({"n": n})                        # latency + ii · (n − 1)
```

`fit_intercept=False` because the constant term is already a basis column whose coefficient *is*
`latency` — nothing is hidden in a separate intercept. The `transform` is the model's own basis map,
used identically at fit and predict, so `predict({"n": n})` evaluates the formula. You set `latency` and
`ii` by hand from an HLS report now (the `seed`); [the fitting section](../calib/) recovers them from a
real RTL simulation.

## Unrolling

Unrolling the loop by a factor `U` processes `U` elements per iteration, so the number of iterations
actually issued — the **effective trip count** — is `m = ceil(n / U)`, not `n`. That is not a fitted
coefficient; it reshapes the basis. Fold it into the `transform`:

```python
transform=lambda r: [1.0, math.ceil(r["n"] / U) - 1],     # m = ceil(n / U)
```

Everything else is unchanged: two parameters `latency` and `ii`, fit the same way. At large `n` the
throughput asymptote `cycles / n → ii / U` is what pins `U` down if it is unknown.

## See also

- [Adding a timing model to a component](./insertion.md) — attaching this model and charging its delay.
- [Block processing](./block.md) / [Streaming processing](./streaming.md) — where in the run loop the
  predicted delay is inserted.
- [Timing model fitting](../calib/) — recovering `latency` and `ii` from measurement.
