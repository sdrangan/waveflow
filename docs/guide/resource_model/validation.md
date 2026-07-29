---
title: Validating a model
parent: Resource Models
nav_order: 3
has_children: false
audience: python
summary: "How to check a resource model honestly: hold out design totals from the fit, lead with decision fidelity rather than relative error, and watch for the way a whole-design number flatters a model whose design is mostly exactly known. Includes the reference results and the traps that make a validation look better than it is."
---

# Validating a model

A resource model's job is to make design decisions without synthesizing. Validation has to test *that*,
which is not quite the same as testing prediction accuracy.

## Hold out the design totals

The per-module figures and the whole-design totals come from the same synthesis runs, but they are not
the same evidence. Fit **only** on per-module figures, and the totals become a genuine held-out test of
whether per-module composition reproduces whole-design synthesis — a stronger claim than any regression
score, because it tests the *composition*, not the fit.

The reference design's results, over 24 points:

| counter | whole-design error | rank correlation vs synthesis |
|---|---|---|
| DSP | **24/24 exact** | 1.000 |
| BRAM | **24/24 exact** | — |
| LUT | 3.2% mean, 8.6% worst | 0.950 |
| FF | 2.8% mean, 8.7% worst | 0.990 |

## Lead with decision fidelity

The question an exploration asks is not *"what is the LUT count"* but *"which design should I pick"*
and *"does this fit"*. Those survive error that a regression table would call poor:

```python
# does the estimate ORDER designs the way synthesis does?
rho = rank_correlation(predicted, measured)

# does it pick the same extremum?
assert argmin(predicted_dsp) == argmin(measured_dsp)
```

A model with 10% LUT error makes every correct choice when candidates are well separated. Asserting
that directly is both the more useful claim and the more defensible one — and it degrades gracefully:
if error rises, you lose the ability to discriminate *close* designs first, which is exactly the
failure mode worth knowing about.

## Two traps

{: .warning }
> **A whole-design number can flatter the model.** The reference LUT error is 3.2% at the design level
> but **9.8% mean / 24.8% worst** for the compute module alone. The difference is not model quality:
> the interface term and the three static modules are *exact*, so they dilute the one fitted module.
> Most of that design is known rather than predicted.
>
> Quote the per-module error when describing the model, and the whole-design error when describing what
> a composed estimate delivers. They are different claims and only one of them is about the model.

{: .warning }
> **In-sample error is not validation.** A fit reproducing its own training data proves only that it is
> not broken. Use leave-one-out, or a genuinely held-out grid point, and report the *worst* case
> alongside the mean — the mean hides the corner that would change a decision.

## Set the tolerance where the measurement is

A test that asserts `error < 50%` passes for years without noticing the model has rotted. Put the
bound just above what the measurement actually produced, so a regression trips it:

```python
assert np.mean(loo["lut"]) < 0.13     # measured 9.8%
assert max(loo["lut"]) < 0.30         # measured 24.8%
```

This is also the honest place to record that a counter is *hard*. LUT is the weakest part of the
approach; the bound says so rather than hiding it.

## Check the model refuses when it should

Validation is not only about accuracy at points inside the grid — a model that answers confidently
outside it is worse than one that answers poorly:

```python
assert fitted.confidence_own(comp_far_outside).level is ConfidenceLevel.EXTRAPOLATED
assert interface.confidence_own(unmeasured_boundary).level is ConfidenceLevel.UNCALIBRATED
```

## What a validation cannot tell you

The models here are built from **HLS estimates** (`source: hls_estimate`). Validating against
whole-design *HLS* totals shows that composition works; it says nothing about whether HLS's own LUT/FF
numbers match post-implementation reality, which they are known not to do closely
([FPGA resources](../resource/xilinx.md)).

That is a separate, later measurement — sample the HLS-vs-Vivado gap at a handful of points — and the
record `source` field exists so it can be added without a schema change. Until then, an estimate is
faithful to what HLS *would report*, not to what the fabric will hold.

## See also

- [Composing a design estimate](./composition.md) — what is being validated.
- [FPGA resources](../resource/xilinx.md) — how far to trust an HLS estimate in the first place.
