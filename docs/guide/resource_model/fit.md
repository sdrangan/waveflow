---
title: Fitting
parent: Resource Models
nav_order: 8
audience: python
api: [fit, load_or_fit, save_model, params_path, corpus, ModuleStore]
summary: "Determining a model's free parameters from measurements, on the resource axis. Where the data is stored (records filed per module key, promoted from an untracked work tier into a committed library), how a sweep produces it, how load_or_fit resolves a published artifact before a corpus, and why installing a model and calibrating one are separate acts. The shared machinery lives in the calibration guide; this page is the resource-specific path through it."
---

# Fitting

**Fitting a model means determining its free parameters from data.** A
[lookup](./lookup.md) fits by memorizing one row per measured configuration; a regression fits by
solving for coefficients. Both are `fit`, and both store their result the same way — which is the
point of the [shared calibration machinery](../calib/), and why this page is short.

What is specific to the resource axis is only *where the measurements come from*: they are synthesis
reports, so producing one costs a toolchain run rather than a simulation.

{: .note }
> A model with **no** free parameters is not fitted at all. A prior whose formula reproduces every
> measured point needs no calibration, and says so by reporting `EXACT` — see
> [confidence](../calib/confidence.md#how-a-model-earns-one). On a Vitis target that covers DSP and
> BRAM, which is why `VitisResourceModel` only ever fits LUT and FF.

## How the data is stored

Measurements are **filed as records**, not transcribed into source. One record per synthesis, keyed
by the module's [elaborated structure](../calib/modules.md#the-key-is-the-structure-not-the-parameters):

```text
<platform>/modules/<module-key>/resource/records.jsonl
```

Two tiers, and the split matters because a sweep is exploratory while a library is reviewed:

| tier | path | tracked? |
|---|---|---|
| work | `calib/work/<platform>/` | no — a sweep re-runs freely and overwrites |
| library | `calib/platforms/<platform>/` | yes — what a model reads |

A deliberate [publish](../platform/workflow.md#publishing-into-one) promotes work into the library, so
an interrupted or experimental run cannot quietly edit committed measurements.

The [corpus](../calib/corpus.md) a fit trains on is **derived** from those records on demand and never
stored twice. Its format, why it records raw facts rather than derived ones, and how many rows you
need are all covered there.

## Producing the data: a sweep

One synthesis gives one point. A [sweep](../build/sweep.md) drives the build DAG across a grid and
files a record per point:

```bash
python -m examples.vecmult.vecmult_sweep --dry-run   # codegen only, no toolchain
python -m examples.vecmult.vecmult_sweep             # the full grid
python -m examples.vecmult.vecmult_sweep --resume    # continue a stopped run
```

Two properties are worth knowing because getting either wrong corrupts the data rather than failing:

**Every point re-runs the whole DAG, forced.** Without that the DAG would see up-to-date artifacts
from the *previous* point and skip, and the report on disk would be attributed to the wrong
configuration. `SweepRunner` forces every run for exactly this reason — you do not pass a flag.

**Choosing the grid is part of choosing the model.** A grid that samples one regime will validate a
law it never tested. If a cost has a threshold in it — a multiply that stops fitting one DSP, an array
that stops fitting block RAM — the grid has to span it, or the fit will look excellent and be wrong on
the other side.

## Running the fit

```python
model.fit()                  # from the corpus the store reduces
model.fit(samples)           # or from [(component, measured_counters), ...]
```

Prefer the first. A hand-built sample list is a second copy of numbers already on disk, and a second
copy is a second thing to keep in step. The explicit form exists for a design whose measurements have
not been filed yet.

**Features are recomputed from each row**, never taken from the caller, so a fit cannot be trained on
a different feature definition than `predict` will evaluate. That is
[`transform`](../calib/model.md#transformparams--derive)'s job and it runs on both paths.

## Loading a fitted model

Installing a model and calibrating one are **different acts**, and conflating them is why a model can
end up refitting on every elaboration. `load_or_fit` keeps them apart by resolving cheapest-first:

```python
model.load_or_fit()          # artifact -> corpus
```

1. **Load** the published artifact if one exists — it predicts with no corpus and no sklearn, which is
   the point of having artifacts at all.
2. **Fit** from `samples` if you passed them. Pass a **callable** and it is only invoked if this step
   is reached, so the common case never pays to elaborate every calibration point.
3. **Fit** from the corpus otherwise.
4. **Neither** — parameters that have no fit behind them report
   [`UNCALIBRATED`](../calib/confidence.md) rather than returning a seed dressed as a measurement.

`save_model()` writes the artifact back. Neither call is told **where**: both default to
[`params_path`](../calib/model.md#where-a-models-data-lives), derived from the model's `name` and
`platform`, so publishing and loading cannot disagree about where a model lives.

That path is keyed by model *name* rather than by module key, because one model serves every
configuration of its class — the same reason [`get_rm`](./getrm.md) is cached per `(class, platform)`.

## Validating {#validating}

**Held out, never in-sample.** Fitted on all your points, even a wrong basis looks good — a model with
as many free parameters as measurements interpolates them exactly and predicts nothing. Leave-one-out
is cheap at these grid sizes and is what the committed gates assert.

Three things worth checking beyond a mean error:

**The direction of the error.** Under-prediction is the one that matters, because it turns *"does not
fit"* into *"fits"*. A known gap should be pinned with a bound rather than left as a footnote, so it
fails if it grows.

**Decision fidelity, not just accuracy.** A resource model exists to answer *does this fit* and *which
of these is cheaper*. A flattering mean error can hide a reversed pair near the budget, and a model
that ranks configurations correctly is more useful than one with a better average that does not.

**The model, not the formulas.** Checking a prior's formula directly can show green counters while the
installed model disagrees with itself — a gap in `VecMult` was invisible until the model was actually
[composed](./predict.md). Test through `compose`, not around it.

## Next

- [Predicting](./predict.md) — using the fitted model over a hierarchy.
- [The corpus](../calib/corpus.md) — the format, and how many rows a fit needs.
- [Sweeping a design](../build/sweep.md) — `ParamGrid`, `SweepRunner` and `sweep_cli` in full.

## See also

- [Model calibration](../calib/) — the shared `fit` / `load_or_fit` / artifact machinery both axes use.
- [Platform workflow](../platform/workflow.md) — the work tier, publishing, and what is committed.
- [The VecMult example](../../examples/vecmult/resmodfit.md) — all of this on a real design, with the
  sweep that produced the numbers.
