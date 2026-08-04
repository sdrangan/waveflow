---
title: The model kinds
parent: Model calibration
nav_order: 5
audience: python
api: [LookupCalibModel, PriorCalibModel, ConcatCalibModel, LinCalibModel, InterpCalibModel, holdout_report]
summary: "The concrete kinds implementing the CalibModel shape, all axis-agnostic: LookupCalibModel memorizes and refuses to interpolate, PriorCalibModel is a zero-parameter formula, LinCalibModel an sklearn regression, InterpCalibModel a 1-D calibrated table, and ConcatCalibModel composes them across targets so each comes from whichever is honest for it."
---

# The model kinds

[What a `CalibModel` is](./model.md) describes the *shape* every model shares. This page is the
kinds that implement it — the ones you actually construct.

**All of them are axis-agnostic.** A kind sees a parameter row and a set of targets; nothing tells it
whether the row came from an RTL run or a synthesis report. That is the claim the harmonization rests
on, and it is checked directly in `tests/calib/test_cross_axis.py`.

## Choosing one

| the target… | kind | free parameters |
|---|---|---|
| follows a **known rule** (a DSP-width threshold, II=1 retirement) | [`PriorCalibModel`](#priorcalibmodel--a-formula) | none |
| was **measured** at the points you care about, and thresholds make interpolation unsafe | [`LookupCalibModel`](#lookupcalibmodel--memorize-and-refuse-to-interpolate) | the table |
| is **affine in its features** (a latency plus a per-iteration cost) | [`LinCalibModel`](#lincalibmodel--linear-least-squares) | one per feature, plus intercept |
| is a **smooth, saturating 1-D curve** you would rather measure than fit | [`InterpCalibModel`](#interpcalibmodel--a-calibrated-lookup) | one per knot |
| is really **several targets of different character** | [`ConcatCalibModel`](#concatcalibmodel--one-model-per-target) | the union |

The axis running through that table is **bias against coverage**. A lookup assumes nothing, so
nothing it assumes can be wrong — but it answers only where you measured. A fit commits to a shape
and, in exchange, answers between the points. A prior commits hardest of all and, when it is right,
is the most believable thing here: it reproduces measurement with *zero* freedom to have been tuned.

## `LookupCalibModel` — memorize, and refuse to interpolate {#lookupcalibmodel--memorize-and-refuse-to-interpolate}

```python
m = LookupCalibModel(basis=["ntap", "samp_w"], target_names=("lut", "ff", "dsp")).fit(corpus)
m.predict_feat({"ntap": 16, "samp_w": 12})     # {'lut': 1600, 'ff': 800, 'dsp': 8}
m.confidence_feat({"ntap": 17, "samp_w": 12})  # UNCALIBRATED — never the nearest entry
```

Its parameters *are* the table, so it fits and stores like any other model. What it will not do is
answer between the points.

{: .note }
> **The refusal is the design.** Resource laws are full of binding thresholds — a multiply that stops
> fitting one DSP, an array that stops fitting block RAM, a loop that stops pipelining at II=1. Across
> one of those, interpolation is not imprecise, it is *wrong*, and a lookup that guessed would be
> confidently wrong exactly where it matters. See [Confidence](./confidence.md#extrapolated-deserves-particular-attention).

Two details that exist because they otherwise bite silently: `4` and `4.0` normalize to the same
point (a CSV round-trip would otherwise create an entry the live side never finds), and a repeated
point **supersedes**, so a re-measurement needs no hand-pruning of the corpus.

[`LookupResourceModel`](../resource_model/lookup.md) is this kind keyed on the **module key** rather
than the parameter tuple — see that page for why the finer key is the safe one.

## `PriorCalibModel` — a formula {#priorcalibmodel--a-formula}

```python
m = PriorCalibModel(formulas={"dsp": lambda f: 2 * f["n_mult"]})
m.predict_feat({"n_mult": 4})     # 8
m.n_free_params()                 # 0
```

For quantities the tool decides *by a rule*. Encode the rule and check it, rather than spending
measurements learning something already known.

A prior reports `EXACT` — and that is a stronger claim than it looks, because it is available to a
model with **zero** free parameters. A formula that reproduces every measured point is more
believable than a regression that fits them, and needs no held-out validation to be believed.

{: .note }
> `fit()` does not move a coefficient — there are none — but it still **checks** the formula against
> the corpus and records the residual. That is what makes the claim falsifiable. A prior that turns
> out to be wrong is a *bug in the rule*, which is a different thing from an uncalibrated model;
> reporting it as `UNCALIBRATED` would hide it among the models that merely lack data.

## `ConcatCalibModel` — one model per target {#concatcalibmodel--one-model-per-target}

The shape most real designs need:

```python
m = ConcatCalibModel(models=(
    PriorCalibModel(name="dsp_rule", formulas={"dsp": lambda f: 2 * f["n_mult"]}),
    LinCalibModel(basis=["n_mult"], target="lut", name="lut_fit"),
))
m.fit(corpus)
m.predict_feat({"n_mult": 6})     # {'dsp': 12, 'lut': 280}
```

DSP follows a device rule exactly; LUT has no closed form and must be regressed. Forcing one kind to
answer for both means either fitting something already known, or asserting a formula where none
exists. This composes them, so **each target comes from whichever model is honest for it**.

Sub-models are in **precedence order** — an earlier one wins a target a later one also claims — and
`get_params` returns the *union* of what they all need, so one corpus row serves every sub-model.

{: .warning }
> **The confidence is the weakest sub-confidence, not an average**, and it names which target sits
> there:
>
> ```text
> level     EXTRAPOLATED
> weakest   ['lut']
> ```
>
> An estimate is believable only to the extent of its least believable part. A concat reporting
> `EXACT` because two of its four targets came from device rules would be the most misleading thing
> this layer could do.

Distinct from [`compose`](../resource_model/predict.md), which sums across the **modules** of a
hierarchy. A concat covers the targets of a *single* model and sums nothing — its sub-models
partition the targets rather than contributing to them.

## The regressions

These two predict **one target** from a **basis** of feature columns, read straight off a
[corpus](./corpus.md) (or a raw `DataFrame`).

Beyond the shared [shape](./model.md), both add the metrics that tell you whether a fit is any good:

| method | purpose |
|---|---|
| `score(data)` | R² of the fitted model on `data` |
| `rel_errors(data)` | per-row `|pred − actual| / |actual|` (skips `actual == 0`) |
| `max_rel_error(data)` | the worst of those |
| `holdout_report(train, test)` | fit on `train`; report `r2_train` + per-row residuals on `test` |

`holdout_report` is the one to reach for when you care about *generalization*: fit on most of the
grid, hold a point out, and read its relative error. A model validated in-sample tells you nothing —
see [Validating](../resource_model/fit.md#validating).

## `LinCalibModel` — linear least squares

[`LinCalibModel(basis, target, fit_intercept=True)`](../../../waveflow/calib/calib.py) is an
`sklearn.LinearRegression` over the basis columns.

```python
from waveflow.calib import LinCalibModel

m = LinCalibModel(basis=["m"], target="cycles").fit(db)   # cycles ≈ intercept + b·m
m.coeffs          # {"m": <slope>, "intercept": <intercept>}
m.predict_feat({"m": 9})
m.score(db)       # R²
m.as_dict()       # serializable {target, basis, coeffs, fit_intercept}
m.plot(db, x_name="m")   # scatter actual vs. fitted line; returns a matplotlib Axes
```

- **`coeffs`** is the fitted `{column: coefficient}` (plus `"intercept"` when `fit_intercept=True`).
- **`fit_intercept=False`** gives a *through-origin* model whose coefficients are the physical
  per-feature rates — e.g. a bus span `setup·num_trans + per_word·nwords` where the two coefficients
  *are* the setup and per-word costs.
- **Non-linear bases are caller-side derived columns**, not a model feature: if you need a `sqrt`
  term, add `db.df["sqrt_nc"] = db.df.n_col ** 0.5` and put `"sqrt_nc"` in the basis. The model stays
  a plain linear fit; the *choice* of basis is yours. (But prefer `InterpCalibModel` to forcing a
  wrong basis onto a measured curve — see below.)

## `InterpCalibModel` — a calibrated lookup

[`InterpCalibModel(basis, target)`](../../../waveflow/calib/calib.py) is piecewise-linear
interpolation over a **single** basis column — a calibrated *lookup*, not a curve fit. It is the
right tool for a quantity that is genuinely non-linear but **smooth and saturating** (e.g. a per-row
pipeline / ping-pong depth as a function of row length): sample it densely enough that linear
interpolation between samples is clean, and it **clamps (flat-extrapolates) beyond the sampled
range** — exactly the saturation behaviour.

```python
from waveflow.calib import InterpCalibModel

g = InterpCalibModel(basis=["n_col"], target="row_depth").fit(db)
g.predict_feat({"n_col": 128})   # interpolated between samples; clamped past the ends (saturation)
g.samples                   # {"feature": "n_col", "x": [...], "y": [...]} — the calibrated table

# or build directly from a stored table (the deserialize path):
g2 = InterpCalibModel.from_samples("n_col", xs=[64, 256, 1024], ys=[69.5, 260.3, 268.5], target="row_depth")
```

Duplicate feature values are **averaged** — so a curve `row_depth(n_col)` measured at several `n_row`
collapses to one value per `n_col`. This is the principled alternative to a `sqrt` fudge: rather than
forcing a basis function the data doesn't obey, you *carry the measurement*.

## No model at all is also an answer

If the target is **deterministic** — one transfer beat per word — carry the constant and skip this
page. A real kernel often uses several stances at once: deterministic occupancy, an exact `II=1`
compute, a prior for DSP, and *one* `InterpCalibModel` for a saturating term. That mixture is what
[`ConcatCalibModel`](#concatcalibmodel--one-model-per-target) exists to present as a single object.

{: .note }
> Every kind takes a **parameter row** through `predict_feat`, never a component. `predict(comp)` is
> the component-facing entry the base composes on top of it, via
> [`get_params`](./model.md#get_paramscomp-runtime--extract-and-record) — which is what guarantees a
> model can only predict from facts the corpus recorded.

## See also

- [What a `CalibModel` is](./model.md) — the shape these implement.
- [A worked example](./example.md) — `LinCalibModel` and `InterpCalibModel` end-to-end.
- [Fitting a timing model](../timing_model/fit.md) — the latency/`ii` line `LinCalibModel` recovers.
- [`waveflow/calib/calib.py`](../../../waveflow/calib/calib.py) — the source (≈300 lines).
