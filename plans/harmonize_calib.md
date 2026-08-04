# Plan: harmonize the resource and timing model stacks onto one `CalibModel`

> **Status (2026-08-03): DRAFT — design sketch, not started.**
>
> Motivating observation: three separate implementations of "where does this model's data live"
> now exist, and the third was added this week without noticing the first two.

## Why

`waveflow.calib` currently holds two parallel stacks that do the same four things — transform inputs,
fit, predict, persist — with almost nothing shared:

| | owns a `LinCalibModel` | persistence | keyed by |
|---|---|---|---|
| `TimingModel` | `_model` | `corpus_path`, `_run_dir`, `collect_rtl`, `collect_pysim`, `get_params` | component **name** + `calib_dir` |
| `FittedResourceModel` | `models: {counter: …}` | *none* | module **key** (content digest) |
| `VitisResourceModel` | same | `save_model`, `load_or_fit`, `platform_artifact` | module key + platform |

The third row was written on 2026-08-03 in ignorance of `CalibModel.save_model` /
`load_model` / `load_or_default`, which already existed, and of `TimingModel.corpus_path`, which
already established a directory convention. That is the concrete cost of the split: the same problem
gets re-solved by whoever touches it next.

Two things already anticipate the unification and should be leaned on rather than rebuilt:

* **`record_store.Record` carries a `target` field**, documented as *"the axis (`resource` or
  `timing`)"*, and records are stored per `(key, target)`. The storage layer is already harmonized.
* **`TimingModel.num_targets` exists**, so multi-target is half-built on that side.

## Design sketch

### The base

``python
@dataclass
class CalibModel:
    name: str                       # identifies the model; also its storage location
    platform: Any = None            # supplies the vocabulary and the storage root
    targets: tuple[str, ...] = ()   # what this model predicts

    # --- where its data lives, derived rather than passed ---------------
    @property
    def data_dir(self) -> Path      # <platform.dir>/models/<name>/
    def corpus_path(self) -> Path   # measured points
    def params_path(self) -> Path   # fitted parameters

    # --- the transform / predict split ----------------------------------
    def transform(self, comp, **runtime) -> dict:      # default: resolved HwParams
    def predict_feat(self, feats: dict) -> dict:       # SUBCLASS IMPLEMENTS -> {target: value}
    def predict(self, comp, **runtime) -> dict:        # base: predict_feat(transform(...))

    def fit(self, samples) -> Self               # returns self, sklearn-style
    def confidence_feat(self, feats) -> Confidence     # SUBCLASS; default UNCALIBRATED
    def confidence(self, comp, **runtime) -> Confidence   # base: confidence_feat(transform(...))
    def save_model(self, path=None); def load_or_fit(self, path=None, samples=None)
``

The user hook is `predict_feat(feats)`, which never sees a component. That is what makes a model
reusable across both axes: the axis-specific part is entirely in `transform`.

### Three asymmetries, and how each is resolved

**1. Runtime inputs.** Resource is a function of the *design* alone. Timing is a function of the
design **and the workload** — `TimingModel`'s features are `nwords` and `num_trans`, which are not
`HwParam`s at all.

*Resolution:* the base signature is `predict(self, comp, **runtime)` and `transform(self, comp,
**runtime)`. A resource transform ignores `**runtime`; a timing transform consumes it. This is the
one place the base is shaped by timing rather than by resource, and it costs resource nothing.

**2. Composition.** Resources compose by **summing over sub-modules**. Timing does not — latency
composes through the dataflow graph (critical path, II, back-pressure), which is not addition.

*Resolution:* composition stays **out of the base** — and out of every model.  `compose(top)` is
already a module-level function that walks `sub_comps` and calls each model; it does not need to be
a method at all.  There is deliberately no `TimingModel.compose`, and the plan should not invent
one.

**3. Keying.** Resource keys records on a content-addressed module key; timing keys a model on a
component *name*.

*Resolution — and this one is cleaner than it looks:* these are **two different keys for two
different things**, and both already exist.

``text
model identity   (name, platform)        -> where its PARAMETERS live
data identity    (module key, target)    -> where its MEASUREMENTS live
``

A model reads *many* records. How many configurations it spans is decided by its **transform**, not
by its storage:

| transform | spans |
|---|---|
| identity (all `HwParam`s) | one row per configuration — a lookup |
| collapsing (basis terms) | every configuration mapping to the same terms |
| workload-consuming | configurations × workloads |

So no keying change is needed. `record_store` already supports it.

### What each stack keeps

| | becomes | keeps | loses to the base |
|---|---|---|---|
| `TimingModel` | a **subclass** | `collect_rtl`, `collect_pysim`, `is_record_valid`, `_to_cycles`/`_to_time`, `placement`, the residual target | `corpus_path`, `_run_dir`, `get_params`, fit/predict plumbing |
| `ResourceModel` | **nothing** — dissolves | — | everything; `compose` was already free-standing |

Domain machinery stays put; only the shared spine moves.  The asymmetry is real rather than
arbitrary: timing has infrastructure that is genuinely about *collecting* timing data, while
"resource-ness" turned out to be nothing more than *which targets* a model predicts.

### Confidence — already shared, and the model for the rest

Confidence is the **one part of the two stacks that never diverged**.  `CalibModel.confidence(row)`
already exists, `TimingModel` already wraps it, and the resource models already return the same
`Confidence` object with the same four levels:

| state | level | what `LinCalibModel` actually says |
|---|---|---|
| never fitted (seed only) | `UNCALIBRATED` | *"LinCalibModel('y') is not fitted"* |
| fitted, query inside the data | `EXACT` / `INTERPOLATED` | *"form reproduces all 6 calibration points exactly (2 free params)"* |
| fitted, query outside the data | `EXTRAPOLATED` | *"n=500 outside [1, 6]; the form did reproduce all 6 fitted points exactly, so the risk…"* |

"Not enough data yet" is therefore already expressed — it is `UNCALIBRATED`, and it says *why* rather
than merely "unknown".  No `NOT_SPECIFIED` level is needed.

That this converged independently on both axes is the strongest evidence the harmonization is
**recovering** a shared design rather than imposing one.  It should survive the refactor untouched;
the only change is the signature, which gains the same shape as `predict`:

```python
predict(comp, **runtime)     = predict_feat(   transform(comp, **runtime))
confidence(comp, **runtime)  = confidence_feat(transform(comp, **runtime))
```

`confidence_feat` gets the same default treatment as `predict_feat`: a subclass that says nothing
inherits the base's honest `UNCALIBRATED`, never a silent high-confidence zero.

*Already gated:* the P0 snapshot records `level` for every one of its 40 points, so any phase that
changes a confidence — not just a number — fails.

### How each attaches to an `HwModule` — and why they differ

The two axes attach *completely differently*, and the harmonized base must not paper over it.

| | how many per module | who calls `predict` | base infrastructure |
|---|---|---|---|
| **resource** | **exactly one** | `compose` walks the tree | `get_rm` / `add_rm` / `resource_model` on `HwModule` |
| **timing** | **zero or many** | the *body*, wherever the delay belongs | **none needed** |

**Resource is a property of the module**, so the base owns the whole path: a classmethod
`get_rm(platform)` returning one model, an `add_rm(platform)` post-order walk installing it, and
`compose` reading `comp.resource_model`.  One module, one model, one number per counter.

**Timing is a property of the *body*.**  A module may have no timing model, or several, and one model
may return **several delays** that the body applies at different points:

``python
delays = self.tm.predict({"n": n})
yield self.timeout(delays[0])      # wait before the first phase
...                                 # do something
yield self.timeout(delays[1])      # wait before the second
``

Nothing generic can know where those `timeout`s belong — only the body does.  So there is **no base
registry to design**: a timing model is an ordinary member the module constructs and calls.

{: .note }
> `HwModule` already has no timing infrastructure — `add_timing_model` lives on `FreeRunMod`, one
> level down, and exists for a narrow reason: attaching a model turns on **per-firing recording**
> (`firing_records`) that `CollectTimingStep` reads back after a pysim run.  That is a
> *calibration-data-collection* hook, not a prediction hook, and it applies only to the one-firing
> loop shape `FreeRunMod` defines.  It should stay where it is.

#### The open problem: getting the platform to a timing model

A resource model receives its platform because `add_rm(platform)` is called from outside, after
construction.  A timing model is built inside `__post_init__`, where there is no platform — so today
designs plumb a `platform_dir` field down through the constructor
(`FirBlock(platform_dir=…)`, `MemRStream(platform_dir=…)`).

**That plumbing is currently a live bug.**  `platform_dir` is an ordinary field, so it participates
in `structure_signature` and therefore in the module key:

``text
platform_dir=None : fir_block-66c56941
platform_dir=A    : fir_block-1a07b531
platform_dir=B    : fir_block-73dc195c
``

The *same design* calibrated against two platforms gets two different keys, so records filed under one
can never be found from the other — and it fails silently, exactly like the unbound-key trap.  A
model's **storage location is not part of the design's identity**, which is the same reasoning that
already excludes `_resource_model` and `_timing_model` from `_CONTEXT_ATTRS`.

*Fix (P1):* add `platform_dir` — and any other calibration-location field — to `_CONTEXT_ATTRS`,
with a test that two platform dirs yield **one** key.

*Then* the plumbing question is a free choice rather than a hazard.  Two options for P3:

1. **Keep the constructor field.**  Explicit, no new machinery, and now safe once the key is fixed.
2. **Bind late, like resource.**  Construct timing models without a platform and have a
   `pre_sim` pass hand it down.  Removes the plumbing from every design, at the cost of models that
   are unusable between construction and `pre_sim`.

Option 1 is the smaller change and matches how designs are written today; option 2 is the one that
scales if timing models become common.  Decide at P3 with the equivalence harness already in place.

### The kinds become axis-agnostic

`Lookup`, `Prior` and `Concat` are all statements about *how a prediction is formed*, not about what
is being predicted. They become `CalibModel` subclasses usable on either axis:

``python
LookupCalibModel(name=…, targets=("lut","ff",…))     # memorize; refuse to interpolate
PriorCalibModel(name=…, formulas={target: fn})       # a formula, zero free parameters
ConcatCalibModel(parts=(…, …))                        # union of disjoint targets
``

`ConcatCalibModel` is the general form of two ad-hoc mechanisms it replaces:
`FittedResourceModel(prior=…)` and `VitisResourceModel`'s monolithic derived+fitted `predict_own`.
It also gives `uncovered()` a natural home — union the parts' targets, compare against the vocabulary
— which today only `VitisResourceModel` implements.

Open: `ConcatCalibModel`'s **corpus** storage. Its parts share one set of measured points but have
different targets. Simplest answer: the concat owns the corpus; parts are fitted from projections of
it and persist only parameters. Needs confirming against `save_model`'s state-dict shape.

## Phases

Each phase ends at a gate that must be green before the next starts.

### P0 — the equivalence harness (do this first)

Record current predictions as golden JSON, before touching anything:

* `vecmult` — 16 corpus points, all counters, plus `compose` totals and confidence levels.
* `fir_block` — 24 corpus points, per-module and composed.
* timing — `tests/calib/test_timing_model.py` + `tests/build/test_calib_steps.py` outputs.

**Gate:** a `tests/calib/test_harmonize_equivalence.py` that passes against the *current* code. Every
later phase must keep it passing byte-identically. Without this the refactor is unverifiable.

### P1 — extend `CalibModel` — **DONE (2026-08-04)**

Add `name`, `platform`, `targets`, path derivation, `transform`/`predict_feat`/`predict`, multi-target
support. Existing single-target `LinCalibModel` keeps working (`targets=(target,)`).

Also in P1, because it is independent and currently wrong: add `platform_dir` to
`_CONTEXT_ATTRS` so a model's storage location stops changing the module key
([why](#the-open-problem-getting-the-platform-to-a-timing-model)).  This is a **behaviour change** —
keys move for any design carrying the field — so the committed `fir_block` corpus keys must be
regenerated, and that is easier before anything else moves.

Follow-ups that landed with it (decisions 6 and 7 below): `corpus()` / `corpus_df` /
`corpus_markdown()` on the base, and `fit(data=None)` reading the corpus by default on **both** axes
(`LinCalibModel`, `InterpCalibModel`, `TimingModel`).  `basis` / `target` gained defaults so the base
is inheritable by a model that has neither.

**Gate:** full suite at its 6-failure baseline; P0 harness unchanged except the deliberate key move,
plus a new test that two platform dirs yield **one** key.  *Result: met — 255 calib tests green, P0
byte-identical.*

### P2 — dissolve `ResourceModel` — **DONE (2026-08-04)**

**`ResourceModel` becomes nothing.** Inspected against the base it adds only `check_counters`,
`counters`, `declared_counters`, `predict_own`, `confidence_own` — every one of which is generic once
`targets` exists. A resource model *is* a `CalibModel` whose targets are the platform's `res_types`:

``python
LookupCalibModel(name="framer", targets=platform.res_types, platform=platform)
compose(top)                      # unchanged — it was always a free function
``

Renames: `predict_own` -> `predict`, `confidence_own` -> `confidence`.  The `_own` suffix marked
"this module only, not its children"; with `compose` a free function that distinction is carried by
the function name.  `check_counters` survives as a check on `targets` — it is what stops a
mistyped counter contributing silently.

`platform_artifact` went too: it was the third hand-rolled path scheme, and `load_or_fit` /
`save_model` now default to the base's derived `params_path`.  Nothing moved on disk — no `models/`
tree had been published yet.

**Gate:** vecmult 16/16 and fir_block 24/24 byte-identical via P0.  *Result: met on the first run —
the dissolution and the 61-site rename changed no prediction.*

**Left for P4/P5:** the resource kinds still override `predict` wholesale rather than going through
`predict_feat(transform(...))`, because a lookup keys on module *identity* and `InterfaceResourceModel`
reads *ports* — neither survives being flattened into a feature vector.

### P3 — `TimingModel` **inherits** from the base — **DONE (2026-08-04)**

Unlike resource, timing has genuine domain machinery that is not model-ness — `collect_rtl`,
`collect_pysim`, `is_record_valid`, cycle<->time conversion, the residual target.  That is a
specialization, so it subclasses.  Move persistence to the base; keep the collection machinery.

What that cost, and what it bought:

* `TimingModel.predict(row)` was renamed **`predict_feat(row)`** (and `confidence` ->
  `confidence_feat`).  It always *was* the numeric half — it takes a feature mapping, never a
  component — and keeping the name `predict` would have meant `predict` denoting one thing on the
  timing axis and another on the resource axis, which is exactly the confusion this plan exists to
  remove.  16 call sites, one of them in library code (`hw_freerun`).
* `component` / `calib_dir` gained defaults so the base's defaulted fields can precede them, with
  `__post_init__` refusing an instance missing either.  Safe because **every** construction site in
  the tree already passes them by keyword.
* `data_dir` is overridden to `calib_dir` rather than the base's `<platform.dir>/models/<name>`: a
  timing model is keyed per *component*.  `corpus_path` and `params_path` still derive from it, so
  the base's storage contract holds and nothing moved on disk.  `params_path` now provably equals
  where the composed `LinCalibModel` writes — asserted in `test_harmonized_base.py` rather than left
  to coincidence.

**Gate:** timing tests + `examples/mem_copy/calibrate_platform.py` produce identical params.
*Result: full suite at its exact 6-failure baseline; 284 calib/timing tests green.*

**Still open from P3:** the docs move (timing-flavoured pages out of `guide/calib` into
`guide/timing_model`), and decision 5 — how a platform reaches a timing model.

### P4 — the axis-agnostic kinds — **DONE (2026-08-04)**

`LookupCalibModel`, `PriorCalibModel`, `ConcatCalibModel`. Retire `LookupResourceModel` and
`PriorResourceModel` as thin aliases first, then remove.

**Done:**

* **`corpus_from_records`** (`record_store.py`) — the resource axis's raw-tier reducer, which is what
  makes "both axes share a corpus format" a fact rather than an aspiration.  Projects a `ModuleStore`
  (keyed per module) onto corpus rows (keyed per feature point).  Filters by `cls_name` by default,
  and the reason is not efficiency: two classes have **different parameter names**, so an unfiltered
  frame is mostly blank columns that no basis can be selected from.  Takes `best()` per module unless
  a `source` is named, so a corpus never mixes an `hls_estimate` and a `vivado_impl` for the *same*
  module.
* **`Record.measured_at`** — optional, omitted from `to_json` when unset, so the 35 committed record
  files round-trip byte-identically and read back with a blank.  A derived corpus carries the blank
  through rather than inventing a plausible date.
* **`LookupCalibModel`** (`calib.py`) — axis-agnostic memorizing lookup, keyed on the `basis` columns.
  Normalizes `4` and `4.0` to one point (a CSV round-trip otherwise creates an entry the live side
  never finds).  Repeated points supersede.
* **`target_names`** on the base — multi-target as a constructor field.  Spelled `target_names`
  because a dataclass field and the derived `targets` property cannot share a name.
* **`_record_fit_summary`** made multi-target: residuals over *every* target, keeping the worst.  A
  mean would let one exactly-reproduced counter mask another the form does not fit, and `EXACT` is
  supposed to be a checked claim.

**Gate:** P0 harness; plus a new test that a lookup fitted on *timing* data behaves identically to one
fitted on resource data — the claim the whole refactor rests on.
*Result: `tests/calib/test_cross_axis.py`, 19 tests — the same class on both axes, plus the reducer
against the real committed `fir_block` store (a lookup over its 26 `FirCompute` rows reproduces every
measured point with zero residual).  Full suite at its 6-failure baseline.*

**Also done — the `get_params` / `transform` split (user-directed, 2026-08-04):**

`transform` now takes a **parameter mapping, never a component**:

* `get_params(comp, **runtime) -> dict` — extraction.  Whatever it returns **is the corpus row**.
* `transform(params) -> dict` — derivation.  Cannot reach anything `get_params` did not record,
  because it never sees the component.

This makes a stored corpus re-fittable *by construction*.  A model that recorded a derived quantity
(`area = a*b*c`) instead of its inputs strands every measurement the moment the derivation is revised;
the signature now makes that unwritable.  It also collapses `LinCalibModel.basis_fn` into
`transform_fn` — one derivation concept, returning a named mapping so a feature and its coefficient
share a name.

`VitisResourceModel` is the case that motivated it: its corpus now stores `mult0_count`,
`xbar0_lanes`, `mem0_banks` (the **declaration**, via `DesignStructure.flatten`) and derives
`xbar_sw` / `xbar_depth` / `n_lane` at fit time via `basis_terms_from`.  Revising the structure->form
dictionary — a modelling claim that certainly will be revised — now re-derives from data already on
disk.  `InterfaceResourceModel`'s boundary read moved to `get_params` for the same reason: its cost
depends on ports and channels that no `HwParam` records, and a fit over an unrecorded boundary could
never be reproduced.

**The rest of P4, completed:**

* **`PriorCalibModel`** — a zero-parameter formula.  `fit()` moves nothing but *checks* the formula
  against the corpus and records the residual, which is what makes "reproduces every point exactly"
  falsifiable rather than asserted.  It reports `EXACT` without a corpus on purpose: the claim is
  *this is the rule the tool follows*, not *this was fitted*, and a wrong prior is a **bug in the
  rule** — reporting it `UNCALIBRATED` would hide it among the models that merely lack data.
* **`ConcatCalibModel`** — sub-models partitioned **across targets**, in precedence order, so DSP can
  come from a device rule while LUT comes from a regression.  `get_params` returns the *union* of
  what the sub-models need, so one corpus row serves all of them — otherwise the params/transform
  guarantee would be reintroduced-and-broken one level up.  Confidence is the **weakest** sub-level,
  naming the target that sits there.  This is the P5 building block.
* **`LookupResourceModel` re-based** onto `LookupCalibModel` per decision 9 — sharing memorization,
  the refusal to interpolate, the confidence transitions and the artifact round-trip, and overriding
  only the identity.  Reconciled with the shared machinery by **recording the module key as a
  parameter** (`get_params`), which the record-store corpus already emits: the specialization costs
  one column rather than a parallel implementation.  It is the first resource kind to predict through
  the base `predict_feat` path.
* **`corpus_from_records` wired in** — `ResourceModel.corpus()` reduces its `ModuleStore` on demand
  (`store=` + `cls_name=`), so a resource model now fits with `fit()` and no arguments, from the
  committed library.  Verified end-to-end: `LookupResourceModel(store=..., cls_name="FirCompute")`
  reads 26 rows off the committed platform and reproduces every one.
* `has_free_params` moved to the base (defaulting `True`), so a concat can count the parameters
  behind an estimate without knowing its sub-models' kinds.

**Docs:** `guide/calib/models.md` rewritten as *The model kinds* — a choosing-one table framed on the
bias/coverage trade, plus a section per kind.

**Gate:** P0 harness; plus a new test that a lookup fitted on *timing* data behaves identically to one
fitted on resource data — the claim the whole refactor rests on.
*Result: met.  `tests/calib/test_cross_axis.py` is 34 tests; P0 byte-identical through the
`LookupResourceModel` re-basing (the risky part — it proves the committed `fir_block` lookups still
resolve); full suite at its 6-failure baseline; `ruff` clean.*

### P5 — re-express `VitisResourceModel` — **MOSTLY DONE (2026-08-04)**

``python
VitisResourceModel  ==  ConcatCalibModel(VitisDerived(...), FabricFit(...))
``

Retire `FittedResourceModel.prior=`. Lift `uncovered()` to the base.

**Done:**

* **`VitisDerived(PriorCalibModel)`** — the zero-parameter half, computing DSP/BRAM/URAM from the
  *recorded structure columns* rather than from a live component, so the device rules can be replayed
  against a stored measurement.  Overrides `confidence_feat` to inherit the block-vs-LUTRAM band's
  doubt instead of a flat `EXACT`.
* **`VitisResourceModel(ConcatCalibModel, ResourceModel)`** — `sub_models()` assembles the derived
  half plus one regression per fitted counter from `fits` (renamed from `models`, which is now
  Concat's tuple).  Its hand-rolled weakest-link arithmetic is **deleted**: that is the shared
  machinery's job now.  What remains local is the part that genuinely needs the platform — the
  uncovered-counter downgrade.
* Each fabric regression carries `transform_fn=DesignStructure.basis_terms_from`, so it is
  self-contained: handed a raw corpus row it derives its own features, exactly as at fit time.
* **`uncovered()` lifted to the base** (`CalibModel.uncovered(vocabulary)`).
* **Every resource kind now predicts from a row** — `predict_feat` / `confidence_feat` on Prior,
  Fitted, Interface and Lookup — so `ResourceModel.predict` is the base composition rather than a
  `NotImplementedError`.  `InterfaceResourceModel` rebuilds its boundary signature from the recorded
  `ports`/`channels`, so the same lookup serves a live instance and a stored row.
* `ConcatCalibModel.sub_models()` added as the indirection that lets a subclass assemble sub-models
  from its own state instead of keeping a parallel tuple in sync.

**Sanctioned golden change:** `vecmult` `vlen512_dw256` moved `INTERPOLATED` -> `EXTRAPOLATED`.  No
number changed.  That point is `LUTRAM_CORNER`, which `test_vecmult.py` already documents as
under-predicted; the range check now runs over recorded parameters, where `mem0_depth=32` is plainly
below the fitted `[64, 8192]`.  The model stopped vouching for a point it was known to get wrong.

**Still open:** retiring the `FittedResourceModel.prior=` **field**.  Every kind is now row-based, so
the composition is expressible — but `fir_compute_fitted().fit(samples)` takes the resource-style
`[(comp, measured), ...]` list, and `ConcatCalibModel.fit` takes a frame.  Closing it needs a small
`ResourceConcat(ConcatCalibModel, ResourceModel)` whose `fit` converts the list via `corpus_row` and
delegates.  Deferred rather than rushed; `prior=` works and is now implemented through the row path.

**Gate:** vecmult 16/16 unchanged; the `SERIAL_PACK_CORRECTION` question from `fir_block` is decided
here — either `ConcatCalibModel` absorbs it as a third part, or `MultGroup` grows a correction field.

### P6 — docs: the section split — **DONE (2026-08-04)**

**The section split (user-confirmed 2026-08-04).**  `guide/calib` accumulated pages from when it was
the *timing* calibration section.  Now that the base is genuinely shared, only the axis-agnostic pages
belong there; the rest move out.  Proposed disposition:

| page | goes to | why |
|---|---|---|
| `model.md`, `corpus.md`, `dataframe.md`, `confidence.md`, `models.md` | **stay** in `guide/calib` | the shared base, corpus format, confidence and kinds — all axis-agnostic |
| `fit.md` ("Fitting a timing model") | `guide/timing_model` | recovers a loop model's `latency`/`ii` |
| `component_residual.md` | `guide/timing_model` | `StreamTimingModel`, the residual method |
| `bus_model.md` | `guide/timing_model` | `BusCalib`, the `m_axi` span law |
| `memstream.md` | `guide/timing_model` | the mem-stream control residual + its fixture |
| `example.md` | **stays** in `guide/calib` (user decision 2026-08-04) | it teaches the *model mechanics* — fit, score, hold a point out — which are axis-agnostic.  That its numbers are cycle counts is incidental: the reader is learning the machinery, not the timing law |
| `modules.md` | `guide/calib` | the record store carries `target="timing"` **and** `target="resource"`; it is genuinely shared infrastructure, not a resource page |
| `resources.md` | `guide/resource_model` | `InspectSynthStep` and csynth attribution — resource-only |

**What it took beyond the `git mv`:**

* `guide/timing_model` is now explicitly **declare it, then calibrate it** — the forward models fix a
  model's *form*, the four incoming pages recover its *numbers*.  The two-level bus-vs-component split
  and the everything-is-cycles note moved with them, since both are timing-specific.
* `guide/calib` was reframed as the shared machinery and nothing else.  Its "direct vs residual" and
  "bus vs component" framing was the pre-harmonization timing view and is gone; a note names the pages
  that moved so an old bookmark lands somewhere useful.
* `resource_model/rm.md` still documented `transform(comp)` returning derived features — the exact
  anti-pattern P5's split exists to prevent.  Rewritten around `get_params` / `transform(params)`.
* Renumbered: `guide/calib` 12, `guide/timing_model` 12.5, `guide/resource_model` 13 — the shared base
  before the two axes that build on it.  Calib's ragged `0.1/0.25/0.4/4/10` became 1..7.

**A guard gap this exposed, now closed.**  The move broke links in thirteen files and
`tests/docs/` stayed green.  `test_anchors_point_at_headings_that_exist` skips a target it cannot
resolve, commented "the file link itself is covered elsewhere" — and it was not.  Added
`test_relative_links_resolve`, which immediately found ten *pre-existing* dead links, three of them in
reader-facing pages (`examples/toy/README.md` pointed at a `guide/components/` section that no longer
exists).  Scoped to `docs/` and live example READMEs; `plans/`, `examples/_archive/` and
`mcp/corpus/` are exempt for stated reasons.

### P6a — the consistency pass — **DONE (2026-08-04)**

By P6 every page has already been written by the phase that settled it (see
[Docs, incrementally](#docs-incrementally)).  What is left is cross-links, the section ordering, and
re-deriving every quoted number from the committed corpora.

**The number gate is now a test.**  `tests/docs/test_documented_numbers.py` recomputes the
load-bearing figures from the committed corpora and matches them against the literal strings in the
pages, so a model change that moves a number fails a test *naming the page to edit*.  Covered:
`vecmult/sweep.md`'s 16-cell BRAM table (checked cell by cell — the *shape* is the claim, and an
aggregate check would pass on a table that got the LUTRAM corner wrong) and `firblock/resource_fit.md`'s
exactness, relative-error and rank-correlation figures.  Verified to bite: perturbing one documented
digit fails the suite.

Scope is deliberately the figures a reader would *act on*, not every integer in the docs — a test
asserting every digit would be edited into uselessness the first time someone rephrased a sentence.
There is also a guard-on-the-guard, because a page rewritten into prose would make every check
vacuously pass.

**One finding, and it was mine rather than the docs':** I first reported `resource_fit.md` as
over-stating its rank correlations (0.950/0.990 vs 0.947/0.989).  It was not.  The repo computes rank
correlation as Pearson over `argsort(argsort(.))` ranks; I had checked against tie-corrected
`scipy.stats.spearmanr`, and the predictions contain ties, so the two conventions disagree in the
third decimal.  The page was right.  Since a digit that depends on an unstated convention is not
reproducible, the convention is now stated on the page and pinned in the test.

Other fixes: the `Next` chain in `guide/calib` had a cycle (`corpus -> confidence -> corpus`) left by
the renumbering; `resource_model/rm.md` still described a two-field `ResourceModel` base and claimed
"every method takes a component", both untrue since P2/P5.  Orphan check: zero guide pages with no
inbound link.

**Gate:** docs guard; no page quoting a number that is not reproducible from a corpus.  *Result: met —
docs suite is 11 checks across two files, full suite at its 6-failure baseline.*

## Docs, incrementally {#docs-incrementally}

Docs are easier to review than code, so they should not all land at the end.  The trick is that
**some pages are stable across this refactor and some are not**:

| stable now — write any time | churns with the API — defer to its phase |
|---|---|
| what a sample is; where measurements come from | which class you construct |
| confidence levels and what they mean | `predict_own` vs `predict` |
| bias vs coverage; when to look up vs generalize | `ResourceModel` existing at all |
| device rules, the structure->form dictionary | `VitisResourceModel`'s internal shape |
| every measured finding (the two BRAM regimes, the LUTRAM corner, the crossbar basis) | — |

A page describing *measurements and ideas* survives the refactor untouched.  A page describing
*signatures* does not.  So: write the first kind early, and pin each of the second kind to the phase
whose gate proves that signature works.

### The target shape

``text
guide/calib/          COMMON to both axes
  index.md            what calibration is; the two axes; the one split
  model.md            the base: name, platform, targets, transform/predict_feat/predict, fit
  samples.md          training data — (component, measurement) pairs, and where they come from
  confidence.md       EXACT / INTERPOLATED / EXTRAPOLATED / UNCALIBRATED, and the weakest-link rule
  storage.md          module keys, the record store, corpora, artifacts   [today: modules.md]
  lookup.md           the memorizing kind
  prior.md            the formula kind
  concat.md           combining kinds by target

guide/timing_model/   TIMING-specific
  index.md, models.md (LT vs CT), insertion.md, loops.md, block.md, streaming.md   [exist]
  collect.md          RTL + pysim collection, the residual target, cycle<->time    [from calib/fit.md]
  residual.md         component residuals, mem-stream, bus model    [from calib/*.md]

guide/resource_model/ RESOURCE-specific
  index.md            targets = res_types; the countable/fitted split
  getrm.md            get_rm — resource only, since timing has no registry
  vitis.md            VitisResourceModel, device rules, structure->form
  predict.md          compose — resource-only composition, the interface term
``

Net: `guide/calib` stops being timing-flavoured (its `fit.md` is titled *"Fitting a timing model"*
today), and each axis keeps only what is genuinely its own.

### Which phase ships which page

| phase | ships | safe because |
|---|---|---|
| **before P0** | `calib/samples.md`, `calib/confidence.md`; the device-rules and structure->form content of `resource_model/vitis.md`; the whole `examples/vecmult` set | pure concepts and measured numbers — no signature dependence |
| **P1** | `calib/model.md`, `calib/storage.md` | the base API is settled by P1's gate |
| **P2** | `resource_model/index.md` rewritten; `rm.md` deleted (its content is now `calib/model.md`) | `ResourceModel` is gone |
| **P3** | `timing_model/collect.md`, `residual.md`; `calib/fit.md` retired | timing is ported |
| **P4** | `calib/lookup.md`, `prior.md`, `concat.md` — **moved** out of `resource_model` | the kinds are axis-agnostic |
| **P5** | `resource_model/vitis.md` updated to the concat form | concat settled |
| **P6** | cross-links, ordering, number re-derivation | — |

Most of `guide/resource_model` as written today survives — it just **moves**.  `samples.md` is already
axis-agnostic; `lookup.md` needs only its resource examples generalized; `rm.md` becomes
`calib/model.md`.

### One ordering fix

Nav order is currently `timing_model` 12, `calib` 13, `resource_model` 13.6 — so the shared base sits
*between* the two things it is shared by.  If `calib` becomes the common section it should come first:
`calib` 12, `timing_model` 12.5, `resource_model` 13.  Do it in P6 with the rest of the ordering.

## Risks

**The docs churn again.**  `guide/resource_model` was rewritten twice this week.  Mitigated rather
than accepted: see [Docs, incrementally](#docs-incrementally).  Most of what exists **moves** rather
than being rewritten, and the pages that describe measurements rather than signatures do not change at
all.  The genuinely API-dependent pages are pinned to the phase that settles them, so nothing is
written twice.

**`calib` becomes component-aware.** `transform(comp)` couples the numeric layer to `hw`. Mitigated by
the fact that `calib.module_key` already imports component machinery, but it should be a deliberate
choice, not a side effect.

**Scope.** Six phases across ~8 modules, two example trees and two doc sections. P0 is what makes it
safe to stop between any two phases.

## Open decisions

9. **"Retire `LookupResourceModel` as a thin alias" — the premise is false.**  Discovered building
   P4.  `LookupCalibModel` keys on the **basis columns**; `LookupResourceModel` keys on the
   **module key**, which is a digest of structure *and* parameters.  The module key is strictly
   stronger: two configurations with identical `HwParam` values but different wiring collide under a
   feature-tuple key and are distinguished under a module key.  So it is a real specialization, not
   duplication, and aliasing it away would silently weaken the resource lookup.
   **Proposed:** `LookupResourceModel(LookupCalibModel, ResourceModel)` overriding `_key` to use the
   module key — shared memorization, refusal to interpolate, confidence transitions and artifact
   round-trip; specialized identity.  Needs the P0 gate to confirm no prediction moves.

1. ~~Does `ResourceModel` own or extend `CalibModel`?~~ **RESOLVED (2026-08-03): neither — it
   dissolves.**  The original sketch said *own*, reasoning that this kept `compose` out of the
   inheritance chain.  That reason was false: `compose` is a module-level function and never was a
   method.  With that gone, `ResourceModel` adds nothing generic, and a wrapper would have forced a
   resource-flavoured adapter per kind — reintroducing the duplication P4 exists to remove.
   `TimingModel` still *inherits*, because its domain machinery is real.
2. **`predict_feat` return shape for a single target** — `{target: value}` uniformly, or a bare float
   when `len(targets) == 1`? Uniform is simpler; bare is kinder to timing's existing callers.
   **Now implemented as bare-when-one** and pinned in `test_cross_axis.py`.  Worth revisiting once
   `ConcatCalibModel` lands: the shape follows `len(targets)`, not the axis, so a *single-counter*
   resource lookup returns a scalar — declared at construction, so a caller knows it statically, but
   it does mean `Concat` must normalize internally rather than assuming a mapping.
3. **`ConcatCalibModel` corpus storage** — see above.
4. **Does the base own `seed`?** `CalibModel` and `TimingModel` both have one; resource models do not
   use it. Probably yes, defaulting to `None`.
5. **Timing platform plumbing: constructor field or late binding?**  Decide at P3; see
   [the open problem](#the-open-problem-getting-the-platform-to-a-timing-model).  Option 1
   (constructor field) is smaller and matches today's designs; option 2 (bind in `pre_sim`) removes
   the plumbing from every design.  Either is safe **only after** the `_CONTEXT_ATTRS` fix in P1.
6. ~~**Does `fit` take samples, or read the corpus?**~~  **RESOLVED (2026-08-04): both, with the
   corpus as the default.**  `fit(data=None)` on every kind; passing data is the explicit override for
   a test or a one-off.  The reason to make the corpus the default rather than the fallback: a fit
   that bypassed it leaves parameters with no answer to *"fitted on what?"*.
7. **Do the two axes converge on one corpus format?**  **Partly resolved (2026-08-04): yes on the
   format, and it is *derived* rather than maintained.**  `corpus.csv` is one row per measurement, a
   column per hardware parameter, per runtime parameter and per target, plus `measured_at` — specified
   in `docs/guide/calib/corpus.md`.  It is a **view** of an axis-specific raw tier (`rtl/`+`pysim/`
   runs for timing, `records.jsonl` for resources), regenerated rather than appended to, so it cannot
   go stale and deleting it is never data loss.  Generation is the one part that stays per-axis,
   because only the axis knows how to read its own raw tier.

   **Still open:** the resource axis does not generate one yet — it has the record store and nothing
   that reduces it to corpus rows.  That is new work this plan did not originally contain; do it in
   P4 alongside `LookupCalibModel`, since a lookup fitted from a corpus is exactly what makes the
   cross-axis equivalence test in P4's gate meaningful.
8. **`fit` on the base: `return self` or `raise NotImplementedError`?** `CalibModel.fit` raises today;
   `ResourceModel.fit` returns `self` as a deliberate no-op.  Take `return self` — "nothing to
   fit" is a legitimate model — and let a subclass that cannot work unfitted say so in
   `predict_feat`.  Annotate `-> Self` (or document "returns `self`") rather than
   `-> "CalibModel"`, which reads as though a new object comes back; it does not, and neither does
   scikit-learn's.
