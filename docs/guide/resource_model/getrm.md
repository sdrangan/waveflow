---
title: Binding a model to a design
parent: Resource Models
nav_order: 4
audience: python
api: [get_rm, add_rm, params_path]
summary: "How a model gets attached to a design: one classmethod, get_rm(platform), returning a model or None for the default lookup. It is a classmethod on purpose — a model must not close over an instance, and having no self makes that impossible rather than merely discouraged. The base caches the result per (class, platform)."
---

# Binding a model to a design

One classmethod on the design says which model prices it:

```python
class VecMult(FreeRunMod):

    @classmethod
    def get_rm(cls, platform):
        ...
```

Return **`None`** — or do not define it at all — to take the default
[lookup](./lookup.md) against the platform's measurement store. That is the right answer whenever you can
afford to measure every configuration you will ask about, so most designs write nothing here.

A model that *derives* its counters also declares
[`resource_structure()`](./vitis.md#what-you-declare); that is a `VitisResourceModel` concept and
lives with it.

## `get_rm(platform)` — which model, on this platform

```python
@classmethod
def get_rm(cls, platform):
    part = getattr(platform, "part", None) or PART
    require_same_device(part, PART, what="VecMult's resource model")
    return VitisResourceModel(
        name="vec_mult", part=part, platform=platform,
    ).load_or_fit(samples=vec_mult_samples)
```

### Why a classmethod

Because a model must not close over an instance, and having no `self` makes that **impossible**
rather than merely discouraged.

The model is handed the component to predict for. The same object has to price every point of a corpus
during [`fit`](./fit.md) and every sibling during [`compose`](./predict.md) — bind it to one instance
and every row of the fit becomes identical, silently.

Everything configuration-specific still reaches the model, just later: through `resource_structure()`
on whatever component it is asked about, at predict time.

### The key is `(class, platform)` — not the parameters

The base caches what `get_rm` returns:

```text
bound to an instance      ✗   breaks fit() and compose()
a class variable          ✗   coefficients depend on the platform
keyed (class, platform)   ✓   one object, cached, prices every configuration
```

Parameters are **absent from the key**, and that is a direct consequence of the model being
instance-agnostic. One `VitisResourceModel` for `VecMult` prices `dwid=64, vlen=4096` and
`dwid=256, vlen=1024` equally well. Had the structure been bound, the key would have needed every
parameter and the cache would be one entry per design point.

### Refuse the wrong platform

`get_rm` is where a platform this class cannot be modelled on gets rejected. Returning a model that
silently applies another technology's geometry is the worse failure — see
[guarding the part](./vitis.md#guarding-the-part).

## What the base does with it

```python
top.add_rm(platform)     # once, on the top — post-order over the whole hierarchy
```

For each module: resolve `get_rm` (cached), install it, or fall back to the store lookup. A module
with **no** model contributes zero *and* reports `UNCALIBRATED` — never silently skipped, because a
missing contribution makes a design read as cheaper than it is.

## Next

- [Predicting](./predict.md) — turning installed models into an estimate.
- [Fitting](./fit.md) — where the coefficients in `load_or_fit` come from.
