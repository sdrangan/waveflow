---
title: What a CalibModel is
parent: Model calibration
nav_order: 1
audience: python
api: [CalibModel, get_params, transform, predict_feat, predict, confidence, fit, targets]
summary: "The base every calibrated model shares, on both the timing and resource axes: get_params extracts what the corpus records, transform derives features from it, then a prediction, a confidence and a fit. Plus where a model's data lives, derived from its name and platform rather than passed."
---

# What a `CalibModel` is

An object that predicts something measurable about a design, and says how much to believe it.

Two axes use it. **Timing** models predict cycles; **resource** models predict LUTs, DSPs and block
RAMs. They differ in what they predict and where the numbers come from — but not in *shape*, which is
what this page describes.

{: .note }
> **You will probably not implement one.** The library supplies the model kinds; a design picks one
> and, if that kind derives its answer from structure, describes what it contains. This page exists
> so the vocabulary in the rest of the section means something.

## The shape

The base class has the following structure:

```python
@dataclass
class CalibModel:
    name: str = ""            # identifies the model, and names its storage directory
    platform: Any = None      # the storage root, and the target vocabulary

    def get_params(self, comp, **runtime) -> dict: ...  # component -> parameters (RECORDED)
    def transform(self, params) -> dict: ...            # parameters -> features
    def predict_feat(self, row) -> float | dict: ...    # a row -> target(s)
    def predict(self, comp, **runtime): ...             # get_params, then predict_feat
    def confidence_feat(self, row) -> Confidence: ...
    def confidence(self, comp, **runtime) -> Confidence: ...
    def fit(self, data=None) -> Self: ...               # defaults to reading the corpus
```

## Constructor

Each `CalibModel` instance must specify two items:

- `name`:  A string model name that will be used for the model storage
- `platform`: a [`Platform`](../platform/identity.md) — **not** a path. Model parameters are specific
  to the device part and synthesis clock the hardware is deployed on, and the platform carries three
  things a bare path could not: its **directory** (where data is stored), its **part** (which device
  rules apply), and its **counter vocabulary** (what targets exist at all).

`platform` may be `None`. The model then has no derived paths and falls back to defaults — which is
what lets a test construct one with nowhere to store anything.


### Two steps, and the boundary between them is the corpus

```text
comp ──get_params──> params row ──transform──> features ──> a number
                          │
                          └── this is what corpus.csv stores
```

```text
predict(comp, **runtime)     =  predict_feat(   get_params(comp, **runtime))
confidence(comp, **runtime)  =  confidence_feat(get_params(comp, **runtime))
```

`transform` is applied *inside* `predict_feat`, so predicting from a live component and predicting
from a row read off disk take the identical path.

## `get_params(comp, **runtime)` — extract, and record

Two sources of input:

- `comp` — the `HwModule`. **The default is the identity**: every resolved `HwParam`, by name.
- `**runtime` — workload inputs that are not properties of the design. Timing needs them (a firing's
  cost depends on `nwords`); resources drop them, because a workload cannot change what was
  synthesized.

```python
>>> model.get_params(comp)
{'depth': 128, 'width': 32}

>>> model.get_params(comp, nwords=256, num_trans=4)
{'width': 16, 'depth': 64, 'nwords': 256, 'num_trans': 4}
```

**Override it to reach past `HwParam`.** `HwParam` is what a module *declares*; a synthesized circuit
also depends on how it was *wired* and what it contains. Those facts are legitimate inputs, and this
is where they enter:

```python
def get_params(self, comp, **runtime):
    params = super().get_params(comp, **runtime)
    params.update(self.structure(comp).flatten())    # multiplier groups, arrays, crossbars
    return params
```

Whatever this returns **is the corpus row**, so the rule for what belongs here is *rawness, not
usefulness*: record the measured facts, never a quantity derived from them.

## `transform(params)` — derive

Takes a **parameter mapping, never a component**. The default is the identity, which is why a model
predicting straight from parameters needs no transform at all. Override it for a derived quantity:

```python
def transform(self, params):
    return {"area": params["a"] * params["b"] * params["c"]}
```

Three things follow.

**It belongs to the model, not the caller.** `fit` and `predict` both go through it, so they cannot
disagree about what an input means — and now they cannot even in principle, because both receive the
same shape.

**Choosing one is a modelling assertion.** Collapsing three parameters into `a·b·c` claims that
nothing but the product matters. That may be exactly right — and if it is, one measurement covers
every configuration sharing a product. It is your claim; the model carries it.

**And because it is a claim, it will be revised** — which is the reason for the split.

{: .warning }
> **Derive in `transform`, never in `get_params`.** A model that recorded `area = a·b·c` instead of
> `a`, `b` and `c` has thrown away the inputs. Revise the claim — decide the product was the wrong
> form — and every measurement is stranded, because nothing on disk can reconstruct the new features.
> Record `a, b, c`; derive the product.
>
> `transform` takes parameters rather than a component precisely so this cannot be got wrong: a model
> **cannot** predict from a fact `get_params` did not record, because it never sees anything else.

The clearest case is [`VitisResourceModel`](../resource_model/vitis.md), whose corpus stores

```text
dwid, vlen, mult0_count, mult0_operand_bits, mem0_banks, mem0_depth, ..., xbar0_lanes
```

— the structure as **declared** — and derives `xbar_sw`, `xbar_depth` and `n_lane` from it at fit
time. The cost of a crossbar in LUTs is exactly the kind of claim that gets revised; storing lane
counts means a revision re-derives from data already on disk.

## `predict_feat(feats)` — inputs to targets

Returns a **scalar** for a single-target model and a **mapping** for a multi-target one — the same
convention scikit-learn uses for multi-output estimators.

`targets` is on **every** `CalibModel` and says which case applies:

```python
>>> model.targets
('residual',)          # one target -> predict_feat returns a scalar
```

A resource model's targets are the platform's counters; a timing model's is its residual.

## `confidence(comp)` — how much to believe it

Every prediction carries one, so a number never travels without its caveat. See
[Confidence](./confidence.md) for the levels and what each means.  Note that there is one confidence level for all targets.

## `fit(samples)` — calibrate from measurements

Reads its [corpus](./corpus.md) — one row per measurement — and returns **itself**,
calibrated, the way a scikit-learn estimator does:

```python
model.fit(samples).predict(comp)     # chains
```

Nothing is copied. What "calibrated" means depends on the kind: a lookup **memorizes** one row per
sample; a regression **fits coefficients**. A model with genuinely nothing to learn returns itself
unchanged.

Both axes now take the same signature — `fit(data=None)`, where the default reads what was measured
and passing a frame is the explicit override:

```python
model.fit()          # reads the corpus — the normal path
model.fit(frame)     # explicit, for a test or a one-off
```

## Where a model's data lives

Derived from `name` and `platform` rather than passed:

```python
>>> m = LinCalibModel(basis=["n"], target="residual", name="mem_r_span", platform=plat)
>>> m.data_dir      # /plat/z20/models/mem_r_span
>>> m.corpus_path   # .../corpus.csv     — the measured points
>>> m.params_path   # .../params.json    — the fitted parameters
```

`name` defaults to `target`. With **no platform** all three are `None` and the model still works —
which is what lets a test build one with nowhere to store anything.

{: .note }
> This derivation replaced **three** hand-rolled schemes that had each solved the same problem
> separately: a corpus path on the timing side, an artifact path on the base, and a third helper on
> the resource side added without noticing the first two. Deriving it once is the point.

### Raw runs versus the corpus

There are **three** kinds of stored thing, and the base owns only the last two:

| | what | who owns it |
|---|---|---|
| **raw runs** | one artifact per measurement — an RTL trace, a `csynth.xml` | the **axis**, because collecting them is axis-specific |
| **the corpus** | the distilled table those runs reduce to | the base — `corpus_path` |
| **the parameters** | what fitting produced | the base — `params_path` |

Timing already works exactly this way:

```text
calib_dir/
  rtl/<run_id>/firings.csv       raw, one directory per RTL run
  pysim/<run_id>/firings.csv     raw, one per pysim run
  corpus.csv                     derived — the two joined on the feature point
  params.json                    fitted
```

`collect_rtl` / `collect_pysim` write the raw side; a `gen_data_frame` step joins them into the
corpus. Only the last two rows are the base's business, which is why the plan leaves collection on
the subclass.

{: .warning }
> **Resources do not use `corpus.csv` today.** They store one `records.jsonl` per
> `(module key, target)` in a [`ModuleStore`](./modules.md) instead — a different shape for the same
> job, because resource measurements are keyed per module while timing measurements are keyed per
> feature point. Whether these converge on one format is an open decision in
> `plans/harmonize_calib.md`; until it is settled, `corpus_path` is used by the timing axis only.

## The component-facing entries take a component, never a feature vector {#takes-a-component}

The design decision the rest of the section follows from, for two reasons.

**Models are heterogeneous.** Different kinds need different things — an identity, a set of
parameters, a description of ports. If the caller had to supply the inputs, it would need to know
every kind's requirements. `get_params` is where that variation lives, which is why *it* takes the
component and `transform` does not.

**One model must serve many components.** During `fit` the same object evaluates every point in a
corpus; when composing an estimate, every module in a hierarchy.

{: .warning }
> A model that closed over one component would return **that** component's inputs for all of them.
> Every row of the fit becomes identical, the regression is rank-deficient, the coefficients are
> meaningless — and nothing raises.
>
> That is why a design supplies its model through a **classmethod**: having no `self` makes the
> mistake impossible rather than merely discouraged.

## Next

- [The corpus](./corpus.md) — the measured data `fit` reads, and where it comes from.
- [Confidence](./confidence.md) — the four levels, and why a composed estimate reports the weakest.
