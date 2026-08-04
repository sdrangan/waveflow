---
title: Confidence
parent: Model calibration
nav_order: 4
audience: python
api: [Confidence, ConfidenceLevel, FitSummary]
summary: "Every prediction carries a level and the facts behind it: EXACT, INTERPOLATED, EXTRAPOLATED or UNCALIBRATED. What each means, how a model earns one, and why a composed estimate reports the weakest link rather than an average."
---

# Confidence

A bare number invites an exploration to optimize into a region nothing ever measured. So every
prediction carries a `Confidence` — a **level**, plus the **facts** behind it.

```python
@dataclass(frozen=True)
class Confidence:
    level: ConfidenceLevel      # EXACT | INTERPOLATED | EXTRAPOLATED | UNCALIBRATED
    facts: dict                 # free-form, but JSON-able
```

This is the one part of the calibration stack that **never diverged** between the two axes: timing
and resource models arrived at the same four levels and the same object independently.

## The four levels

| level | means |
|---|---|
| `EXACT` | the model's form reproduces every calibration point with **zero residual** — a claim, and a checked one |
| `INTERPOLATED` | the query lies inside the region the model was fit over |
| `EXTRAPOLATED` | outside it |
| `UNCALIBRATED` | no fit backs this number — it came from a seed, a default, or nothing at all |

They are ordered, which is what lets a composed estimate take a minimum.

## How a model earns one

The transitions, from a real `LinCalibModel`:

```python
m = LinCalibModel(basis=["n"], target="y", seed={"coeffs": [2.0], "intercept": 1.0})
m.confidence_feat({"n": 10})
# UNCALIBRATED — "LinCalibModel('y') is not fitted"
```

```python
m = LinCalibModel(basis=["n"], target="y").fit(six_points)
m.confidence_feat({"n": 3})
# EXACT — "y: form reproduces all 6 calibration points exactly (2 free params)"

m.confidence_feat({"n": 500})
# EXTRAPOLATED — "y: n=500 outside [1, 6]; the form did reproduce all 6 fitted points exactly…"
```

So **"not enough data yet" is already expressed** — it is `UNCALIBRATED`, and it says *why* rather
than merely "unknown". There is no separate "not specified" level, and none is needed.

{: .note }
> `EXACT` is a stronger claim than it looks, and it is available to a model with **zero** free
> parameters. A formula derived from device geometry that reproduces every measured point is more
> believable than a regression that fits them — and it needs no held-out validation to be believed.

## `EXTRAPOLATED` deserves particular attention

Leaving the measured region usually means crossing a **binding threshold** — a multiply that stops
fitting one DSP, an array that stops fitting block RAM, a loop that stops pipelining at II=1. Those
move several targets at once rather than degrading smoothly.

That is also why a [lookup](./model.md) refuses to interpolate rather than returning its nearest
entry: between two measured points, the truth can be anywhere.

## The facts

`facts` is deliberately model-specific — only `level` is guaranteed. One key is conventional:
**`summary`**, a one-line human string. `to_json()` flattens the whole thing to
`{"level": …, **facts}`, which is what a report or an agent consumes.

```python
{'level': 'EXACT',
 'summary': 'Blk: dsp from an analytical prior with no fitted parameters',
 'module_key': 'blk-22e53744',
 'model': 'prior',
 'counters': ['dsp'],
 'inputs': {'area': 4096}}
```

There is no schema to conform to: put in whatever a reader would need in order to judge the number.
Keys other kinds add include `measured_source` (which synthesis a lookup's number came from),
`per_counter` (one nested confidence per target), and `uncovered` (targets this model does not
predict at all).

## A composed estimate reports the weakest link

When predictions are summed — over the modules of a hierarchy, or over the targets of one model — the
result takes the **minimum** level, not an average, and names what sits there:

```text
total   {'lut': 1370, 'ff': 597, 'dsp': 4, 'bram': 4}
level   INTERPOLATED
weakest [('vec_mult', 'VecMult')]
```

That example has two targets from zero-parameter rules that reproduce every measurement, and two from
a regression. It still reports `INTERPOLATED`. An estimate claiming `EXACT` while half of it is a fit
would be the most misleading thing this layer could do.

`weakest()` is the actionable half: it is the list of things you would recalibrate first.

## Nothing is silently zero

Two failure modes get the same treatment, because they are the same failure:

- a **module** with no model contributes zero **and** reports `UNCALIBRATED`, naming itself;
- a **target** a model does not cover is reported by name, never defaulted to zero.

Under-counting is the one direction an estimate must not err — it turns *"does not fit"* into
*"fits"*. Both cases say so out loud instead.

## Next

- [The model kinds](./models.md) — the concrete models, and how the choice between them decides which
  of these levels you can expect.
