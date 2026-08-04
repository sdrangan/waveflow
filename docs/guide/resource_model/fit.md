---
title: Fitting
parent: Resource Models
nav_order: 7
audience: python
api: [load_or_fit, save_model, fit, basis_terms, params_path]
summary: "Where the fabric half's coefficients come from. The basis is derived from the structure declaration rather than invented, so a bad held-out error means a missing structure and not a missing polynomial term. load_or_fit resolves published artifact first and local corpus second, so installing a model and calibrating one stay separate acts. Validate held out, and lead with decision fidelity rather than relative error."
---

# Fitting

Only the fabric half is fitted. DSP, BRAM and URAM come from
[device rules](./vitis.md#the-device-rules) with zero free parameters — nothing about them needs
calibrating, and a formula that reproduces every measured point is a **stronger** claim than a
regression that fits them.

## The basis comes from the declaration

You do not choose basis functions. The fabric structures you already declared in
[`resource_structure()`](./vitis.md#what-you-declare) *are* the features:

| declared | basis term |
|---|---|
| `PerLane(lanes)` | `n_lane` |
| `Crossbar(lanes)` | `xbar_sw`, `xbar_depth` |
| `Counter(over)` | `addr_bits` |
| `ReductionTree(lanes)` | `reduce_ops` |

Terms accumulate across instances under fixed names, so two crossbars of different widths sum rather
than requiring you to invent a term.

This is the structure→form dictionary as arithmetic, and the reason there is no second place to state
the basis:

{: .warning }
> **A bad held-out error means a missing structure, not a missing polynomial term.** You fix it by
> going back to the body and asking what is physically there that you did not declare — not by adding
> `p²` until the residual falls. That discipline is only enforceable because the basis has exactly one
> source.

On `VecMult` the payoff is concrete. The obvious features — the raw parameters `dwid` and
`log2(vlen)` — reach **43% error on LUT and 52% on FF**. They assume a per-lane datapath and a
counter, and miss the crossbar entirely. Declaring the `Crossbar` puts `LW²` and `LW²·log2(LW)` in the
basis, and held-out error drops to **0.00% on LUT**. Same measurements, same fitter; the difference is
one line of declaration.

## Building a corpus

`fit` takes `[(component, measured_counters), ...]`:

```python
def vec_mult_samples() -> list:
    return [(elaborate(VecMult, {"dwid": d, "vlen": v}, name="fit"), m)
            for v, d, m in in_bram_points()]
```

Two things worth copying.

**Features are recomputed from each component**, never taken from the caller — so the fit cannot be
trained on a different feature definition than `predict` will evaluate.

**Train on one regime only.** `in_bram_points()` excludes the point where HLS put the buffer in LUTRAM
instead of block RAM. Asking one line to span a discontinuity does not make it better at the
discontinuity; it makes it worse everywhere else. A regime the prior *predicts* should not also be
smoothed over by the fit.

### Where the numbers live

Commit the corpus as **source**, not as the sweep's JSON. `results/*.json` is untracked, so committing
the numbers is what makes a measurement outlive the work directory — and it is what lets the model
gates run with **no toolchain installed**, so a machine without Vitis can still catch a model that
stopped reproducing its own corpus.

## `load_or_fit` — install and calibrate are different acts

```python
VitisResourceModel(name="vec_mult", part=part, platform=platform).load_or_fit(
    samples=vec_mult_samples,
)
```

Resolution order:

1. **Load** the artifact if it exists — a published artifact predicts with no corpus and no sklearn,
   which is the point of having artifacts at all.
2. **Fit** *samples* otherwise. Pass a **callable**, so the common case (an artifact exists) never
   pays to elaborate every calibration point.
3. **Neither** — the derived half still answers exactly; the fitted half reports `UNCALIBRATED` rather
   than returning a seed dressed as a measurement.

`save_model()` writes the artifact back. Neither call is told **where**: both default to
[`params_path`](../calib/model.md#where-a-models-data-lives), derived from the model's `name` and
`platform`, so publishing and loading cannot disagree about where a model lives.

That path is keyed by model *name* rather than by module key, because one model serves every
configuration of its class — the same reason `get_rm` is cached per `(class, platform)`.

## Validating {#validating}

**Held out, never in-sample.** Four parameters will fit fifteen points comfortably and tell you
nothing. Leave-one-out is cheap at this scale and is what the committed gates assert:

```python
def test_crossbar_basis_predicts_lut_and_ff_held_out():
    for target, mean_lim, max_lim in (("lut", 0.001, 0.001), ("ff", 0.01, 0.02)):
        mean_err, max_err = _loo_error(A, y)
        assert mean_err <= mean_lim and max_err <= max_lim
```

**Pin why the basis is what it is.** A test that the *naive* basis is >30% wrong is as valuable as one
that the real basis is right — without it the quadratic terms read as unexplained curve-fitting, and a
later "simplification" back to linear-in-width looks harmless.

**Watch the direction of error.** Under-prediction is the one that matters: it turns "does not fit"
into "fits". `VecMult` under-predicts LUT by 1.8% at the LUTRAM corner — the prior correctly returns
0 BRAM there, but the fit has no term for a buffer that became registers. That is pinned with a bound
rather than left as a footnote, so it fails if it grows.

**Lead with decision fidelity.** A resource model exists to answer *does this fit* and *which of these
is cheaper*. A headline mean error can flatter a model that gets those wrong at the one point an
exploration cares about — and a model that ranks configurations correctly is more useful than one with
a better average and a reversed pair near the budget.

{: .note }
> **Validating the formulas is not validating the model.** Checking `dsp_prior` and `bram_prior`
> directly can show four green counters while the installed model disagrees with itself — the
> `VecMult` corner gap was invisible until the model was actually [composed](./predict.md). Test
> through `compose`, not around it.

## Next

- [The VecMult example](../../examples/vecmult/resource_model.md) — all of this on a real design, with
  measured numbers and the sweep that produced them.
- [The block FIR](../../examples/firblock/resource_fit.md) — the advanced case: a composite, with an
  interface term and a second basis.
