---
title: Composing a design estimate
parent: Resource Models
nav_order: 2
has_children: false
audience: python
api: [compose, ResourceEstimate, InterfaceResourceModel, boundary_signature]
summary: "compose() walks the elaborated graph applying own-plus-children. Covers the interface term and why it is keyed on boundary structure rather than parameters (measured: invariant across 24 compute configurations, and it moves when the memory width does), a module with no model being reported rather than skipped, and weakest-link confidence naming what to recalibrate first."
---

# Composing a design estimate

```python
from waveflow.calib.resource_model import compose

est = compose(top, model_for)      # top = elaborate(FirBlock, params)

est.total          # {'lut': 9424, 'ff': 11398, 'dsp': 32, 'bram': 2}   <- predicted
                   # measured for this point:  8674 / 11347 / 32 / 2
est.own            # {'lut': 1984, 'ff': 1949, 'dsp': 0, 'bram': 2}     the interface term
est.level          # INTERPOLATED -- the weakest confidence that fed it
est.weakest()      # [(path, cls_name, Confidence), ...] -- what to recalibrate first
```

The measured figures are shown beside the prediction deliberately: DSP and BRAM land exactly, LUT is
out by 8.6% — which happens to be this model's *worst* point across the grid. A documentation example
that only showed the number the model produced would read as agreement.

`model_for` maps a component to its [model](./models.md). `compose` walks the elaborated graph, applies
`own + Σ children`, and accumulates both the counters and the confidences.

## The interface term

A composite's own cost is keyed on **boundary structure**, not on its parameters:

```python
boundary_signature(top)      # ((port kind, width), ...), ((channel width, depth), ...)
```

That is not a modelling preference — it is what the measurements say, in both directions:

| | serial | unroll |
|---|---|---|
| `mem_dwidth=32` | 1984 / 1949 / 2 | **1984 / 1949 / 2** |
| `mem_dwidth=64` | 2356 / 2057 / 4 | **2356 / 2057 / 4** |

*(LUT / FF / BRAM.)* Across a 24-point sweep of `ntap`, `samp_w` and realization the term **never
moved**. It moved when `mem_dwidth` changed — identically for both realizations, with BRAM doubling as
the adapter buffers widened. The glue depends on the boundary and not on what the modules compute.

{: .note }
> **It is a lookup, not a fit — and that is a statement about the evidence.** The natural next form is
> a decomposition, `Σ adapter_cost(kind, width) + Σ fifo_cost(width, depth) + shell`, which is exactly
> what the signature is shaped to support. Separating those coefficients needs more boundary
> configurations than the two measured so far; fitting them from two points would be inventing
> structure rather than finding it. Characterizing them **once per platform** — the move
> [`BusCalib`](../calib/bus_model.md) already makes for the bus law — is the upgrade path.

## A missing model is reported, not skipped

```python
est = compose(top, lambda c: None)
est.level                                  # UNCALIBRATED
[c.summary for _, _, _, c in est.per_module]
# ['FirCompute has no resource model; its cost is missing from this estimate, not zero', ...]
```

A module silently contributing zero would make a design look **cheaper** than it is — the one direction
of error an area estimate must never make, since it turns "does not fit" into "fits".

## Weakest-link confidence

A composed estimate is only as good as its worst part, so `est.level` is the minimum over the modules
that fed it, and `est.weakest()` names them:

```python
est = compose(_top(ntap=256, samp_w=16, unroll=False), model_for)
est.level                                   # EXTRAPOLATED -- ntap far outside the fitted 8..32
[n for _, n, _ in est.weakest()]            # ['FirCompute']
```

That is the actionable output: not "the estimate is uncertain" but "*this module* is the reason, and it
is the one to synthesize next."

## Own cost can be negative

If HLS shares logic across a module boundary, a composite's own term goes below zero. Nothing clamps
it.

{: .warning }
> A negative own-cost is the signal that additivity is **leaking** — the modules are not as separable as
> the model assumes. Clamping it at zero would hide precisely the cross-block surprise that whole-design
> synthesis exists to catch, and would do so while making the arithmetic look tidier.

## A worked model set

The reference design's five models, showing the expected shape — one fit, three lookups, one interface:

```python
def model_for(comp):
    cls = type(comp).__name__
    if cls == "FirCompute":   return prior_plus_fitted      # the only module that moves
    if cls == "FirBlock":     return interface_model        # the composite's own term
    if cls in STATIC:         return LookupResourceModel(...)
    return None                                             # reported, not skipped
```

## See also

- [The model kinds](./models.md) — what goes in `model_for`.
- [Validating a model](./validation.md) — checking the composed total against a real synthesis.
- [Composite kernels](../resource/composite.md) — the measurement this composition mirrors.
