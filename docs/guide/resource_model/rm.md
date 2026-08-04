---
title: What a ResourceModel is
parent: Resource Models
nav_order: 1
audience: python
api: [ResourceModel]
summary: "The interface every resource model implements — a CalibModel whose targets are the platform's counters: get_params extracts what the corpus records, transform derives features, then predict and confidence. What counters are and why you do not define them, and which model kind to reach for. Most designs implement none of these methods — they pick a kind and let it do the work."
---

# What a `ResourceModel` is

An object that answers one question about one module:

> Given this elaborated component, what does **it alone** cost, and how much should I believe that?

"It alone" is load-bearing: a model never accounts for its children — that is
[composition's](./predict.md) job.

{: .note }
> **You will probably not implement any of this.** The methods below are the *interface*; the
> library supplies the model kinds that implement them, and a design just picks one in
> [`get_rm`](./getrm.md). This page exists so the vocabulary in the rest of the section means
> something — each kind gets its own page.

## The interface

```python
class ResourceModel(CalibModel):
    def get_params(self, comp, **runtime) -> dict: ...  # component -> parameters (RECORDED)
    def transform(self, params) -> dict: ...            # parameters -> features
    def predict(self, comp) -> dict: ...
    def confidence(self, comp) -> Confidence: ...
    def fit(self, samples=None) -> "ResourceModel": ...
```

A `ResourceModel` **is** a [`CalibModel`](../calib/model.md) whose targets are the platform's
counters. Everything generic — the storage paths, the corpus, confidence — comes from that base;
what is resource-flavoured is the counter vocabulary and a prediction that is a *mapping* rather than
a scalar.

### `get_params(comp)` — extract, and record

**The default is the identity** — every resolved `HwParam`, by name:

```python
>>> model.get_params(comp)
{'depth': 128, 'width': 32}
```

Override it to reach past `HwParam`, for a fact that shapes the hardware but is not declared — a port
count, a channel width, a declared multiplier group. `InterfaceResourceModel` does exactly this, and
must: a composite's own cost depends on its ports and channels, which no `HwParam` records.

Whatever it returns **is the corpus row**, so keep it raw.

### `transform(params)` — derive

Takes a **parameter mapping, never a component**. Override it for a derived quantity:

```python
def transform(self, params):
    return {"area": params["a"] * params["b"] * params["c"]}
```

**Choosing a transform is a modelling assertion.** Collapsing three parameters into `a·b·c` says
*"nothing but the product matters"*. That may be exactly right — and if it is, one measurement covers
every configuration sharing a product. It is your claim to make; the model simply carries it.

{: .warning }
> And because it is a claim, it will be revised — so derive here and **record the inputs** in
> `get_params`. A model that stored `area` instead of `a`, `b`, `c` strands every measurement the
> moment the product turns out to be the wrong form. See
> [the corpus](../calib/corpus.md#raw-not-derived).

Full detail on the split, and why `transform` cannot see the component, is in
[What a `CalibModel` is](../calib/model.md).

### `predict(comp)` — the counters

Returns `{counter: value}` for this module alone. Takes the **component**, never a feature vector —
[see below](#takes-a-component).

### `confidence(comp)` — how much to believe it

Returns a [`Confidence`](./predict.md#confidence): a **level** plus **facts** explaining it.

```python
@dataclass(frozen=True)
class Confidence:
    level: ConfidenceLevel      # EXACT | INTERPOLATED | EXTRAPOLATED | UNCALIBRATED
    facts: dict                 # free-form, but must be JSON-able
```

`facts` is deliberately model-specific — only `level` is guaranteed. One key is conventional:
**`summary`**, a one-line human string, which `Confidence.summary` falls back to a generated string
for if absent. `to_json()` flattens the whole thing to `{"level": …, **facts}`, which is what an agent
or a report consumes.

A real one:

```python
{'level': 'EXACT',
 'summary': 'Blk: dsp from an analytical prior with no fitted parameters',
 'module_key': 'blk-22e53744',
 'model': 'prior',
 'counters': ['dsp'],
 'inputs': {'area': 4096}}
```

There is no schema to conform to: put in whatever a reader would need in order to judge the number.
Each model kind documents the keys it adds.

### `fit(samples)` — calibrate from measurements

Takes a [corpus](../calib/corpus.md) — or, on the resource side, pairs of *(component, measured
counters)* — and returns itself, calibrated. With no argument it reads what was measured: a model
given a `store=` reduces its [record store](../calib/modules.md) on demand. What "calibrated" means depends on the kind: a lookup **memorizes** one row per sample; a
Vitis model **regresses** its fabric coefficients and derives the rest from structure.

A model with genuinely nothing to learn returns itself unchanged.

## Counters — you do not define these {#counters}

Three related things, and **none of them is yours to write**:

| | what it is | who supplies it |
|---|---|---|
| the **vocabulary** | which counters exist at all — `lut`, `ff`, `dsp`, `bram`, `uram`, `srl` | the [platform](../platform/identity.md), because a counter set is a property of the technology |
| `declared_counters()` | which of them *this model* predicts | the model kind, automatically |
| `check_counters()` | refuses a counter outside the vocabulary | the base |

The platform reaches the model because it **is a constructor argument** on the shared base, and every
kind inherits it:

```python
@dataclass
class CalibModel:
    name: str = ""              # identifies the model, and names its storage directory
    platform: Any = None        # supplies the counter vocabulary; None -> the FPGA default
    ...
```

```python
def counters(self) -> tuple:
    return tuple(self.platform.res_types) if self.platform is not None else COUNTERS
```

So `LookupResourceModel(store=…, platform=platform)` and
`VitisResourceModel(name=…, part=…, platform=platform)` are both just passing that base field through.
With `platform=None` a model still works — it falls back to the built-in FPGA counter set, which is
what lets tests construct models with no platform at all.

`declared_counters()` is derived rather than written — each kind computes it from what it was given.

{: .note }
> **Why the check exists.** Without it a mistyped counter predicts fine in isolation and is silently
> dropped when counters are summed — so the module contributes **zero**. A missing contribution makes
> a design read as *cheaper* than it is, which is the one direction an estimate must not err: it
> turns "does not fit" into "fits".

## The component-facing entries take a component, never features {#takes-a-component}

`predict(comp)` and `confidence(comp)` take the elaborated module; `transform(params)` deliberately
cannot see it. The split is the [params/transform contract](../calib/model.md) — what follows is why
the *outer* entries are shaped this way.

**Models in one hierarchy are heterogeneous.** Different kinds need different things — an identity, a
set of parameters, a description of the ports. If [`compose`](./predict.md) had to supply the inputs,
it would have to know every kind's requirements. `get_params` is where that variation lives, which is
why *it* takes the component.

**One model must price many components.** During [`fit`](./fit.md) the same object evaluates every
point in a corpus; during `compose`, every sibling in a hierarchy.

{: .warning }
> A model that closed over one instance would return **that instance's** features for all of them.
> Every row of the fit becomes identical, the regression is rank-deficient, the coefficients are
> meaningless — and nothing raises.
>
> That is why [`get_rm`](./getrm.md) is a **classmethod**: having no `self` makes the mistake
> impossible rather than merely discouraged.

## Next

- [Samples](../calib/corpus.md) — the training data every `fit` takes, and where it comes from.
- Then the kinds themselves, starting with [the lookup model](./lookup.md).
