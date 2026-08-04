---
title: The lookup model
parent: Resource Models
nav_order: 3
audience: python
api: [LookupResourceModel, fit, ModuleStore, InspectSynthStep]
summary: "The simplest model: resource usage looked up directly from prior synthesis results for exactly those parameter values. State which resource types to store, fit it on measured points, and it answers EXACT for a configuration it has seen and UNCALIBRATED for one it has not. It does not interpolate, and that is deliberate."
---

# The lookup model

The **simplest** resource model: resource usage is looked up directly from prior synthesis results
for exactly those parameter values.

When there is no prior synthesis result with an exact parameter match, it reports a confidence of
`UNCALIBRATED` — it does not interpolate.

It is a machine-learning model like any other. Its training data is pairs of *(parameter values,
measured resources)*; its fitted parameters are the table those pairs become. The only unusual thing
about it is that it refuses to predict between the points it was given.

## Creating and fitting one

State which resource types to store, then fit it on measured points:

```python
from waveflow.calib.platform import VITIS_RES_TYPES
from waveflow.calib.resource_model import LookupResourceModel

rm = LookupResourceModel(res_types=VITIS_RES_TYPES)
rm.fit(samples)
```

[`samples`](../calib/corpus.md) is `[(component, measured_resources), ...]` — the same shape every model's
`fit` takes, one pair per configuration you measured. The measurements come straight out of a
synthesis report, so the report's own spelling (`LUT`, `BRAM_18K`) is accepted.

In practice you rarely build that list yourself: the build files the measurements for you, and the
default lookup reads them back. [Samples](../calib/corpus.md) covers all three routes.

## Using it

```python
for w in (32, 64, 128):
    comp = elaborate(Framer, {"dwid": w}, name="f")
    rm.predict(comp), rm.confidence(comp).level
```

```text
  dwid=32    {'lut': 251, 'ff': 198}    EXACT
  dwid=64    {'lut': 402, 'ff': 331}    EXACT
  dwid=128   {}                         UNCALIBRATED
```

`dwid=128` was never synthesized. Rather than guessing from the two neighbours, the model says so:

```text
Framer: no measurement stored for key framer-51909685; a lookup cannot interpolate,
so this is a gap, not an estimate
```

{: .note }
> **Why refuse to interpolate?** Resource costs are full of *binding thresholds* — a multiply that
> stops fitting one DSP, an array that stops fitting block RAM — where the answer jumps rather than
> slopes. Between two measured points the truth can be anywhere. A model that interpolated would be
> confidently wrong exactly where it matters, so this one declines.
>
> To predict *between* points you need a model that knows why the numbers move: see
> [`VitisResourceModel`](./vitis.md).

## Usually you write none of this

If a module has no [`get_rm`](./getrm.md) at all, the base installs a lookup backed by the platform's
measurement store:

```python
LookupResourceModel(store=ModuleStore(platform.dir), platform=platform)
```

and `InspectSynthStep` — the rung after `csynth` in a build DAG — files a record for every module on
**every** synthesis. So the ordinary workflow needs no model code:

> **Build a configuration, and it becomes predictable.**

Sweep the configurations you care about and the lookup covers them. One you never built stays honestly
uncalibrated.

Constructing one by hand, as above, is for the case where the numbers should live in **source** rather
than in a platform directory — a committed corpus, so model tests run on a machine with no toolchain
installed.

## When to use a lookup

A lookup is the **lowest-bias** model available. It stores each configuration independently, so it can
represent *any* mapping from parameters to resources — including one that jumps, doubles, or drops to
zero between neighbouring points. It assumes nothing about the shape of the function, so no structural
assumption can be wrong.

What it gives up is **generalization**. It predicts nothing between the points it holds, so its sample
complexity is the size of the parameter space you intend to query: one synthesis per point.

So the criterion is *not* "does this module's cost stay constant?" — it may vary arbitrarily. It is:

1. can you **enumerate** the configurations you will ask about, and
2. are you willing to **synthesize each one** — or have they already been synthesized?

If yes, a lookup is the safest model you can use: exact everywhere it answers, honest everywhere else.

If the space is too large to enumerate — a multi-parameter sweep, a design-space exploration over
thousands of points — you need a model that **generalizes**, and generalizing means committing to a
shape. That is bias you accept in exchange for coverage, and it is
[`VitisResourceModel`](./vitis.md)'s trade.

{: .note }
> The case where a lookup is *obviously* right is when the configurations are few — often one. In
> `examples/fir_block`, three of the four modules kept identical resource numbers across a 24-point
> sweep, because none of the swept knobs reached them: one measurement each, total coverage.
>
> That is a happy special case, not the criterion. A module whose cost varies wildly is still a
> lookup if you can afford to measure every point you will ask about.

## Next

- [Binding a model to a design](./getrm.md) — how a model gets installed.
- [`VitisResourceModel`](./vitis.md) — when the space is too large to enumerate.
