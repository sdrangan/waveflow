---
title: Model calibration
parent: Guide
nav_order: 12
has_children: true
audience: python
api: [CalibModel, CalibDataFrame, LookupCalibModel, PriorCalibModel, ConcatCalibModel, LinCalibModel, InterpCalibModel, ModuleStore, Confidence]
summary: "The machinery both model axes share. One CalibModel base: get_params extracts what the corpus records, transform derives features, predict_feat answers, and every answer carries a Confidence. One corpus format (corpus.csv, one row per measurement, derived from each axis's raw tier). One set of model kinds — lookup, prior, concat, and two regressions. Timing and resource models are the same class; only the source of a number differs."
---

# Model calibration

A model here predicts something measurable about a design — how many cycles a firing takes, how many
LUTs a module costs — and says how much to believe the answer. Those numbers are properties of the
*synthesized* hardware, so they are **recovered from measurement** rather than asserted: run the
kernel through synthesis or RTL cosim, record what happened, fit a model, and the fast Python estimate
tracks the slow truth without anyone transcribing report numbers by hand.

**This section is the machinery, not either axis.** Timing and resource models are the same class:
they differ in what they predict and where their numbers come from, but not in shape. What follows is
that shape, once — the two axes then apply it in [Timing Models](../timing_model/) and
[Resource Models](../resource_model/).

## Start here

1. [What a `CalibModel` is](./model.md) — `get_params` → `transform` → `predict_feat`, plus
   confidence, fit, and where a model's data lives.
2. [The corpus](./corpus.md) — the measured data every `fit` reads, and its one canonical shape.
3. [`CalibDataFrame`](./dataframe.md) — the object implementing that format.
4. [Confidence](./confidence.md) — the four levels, and why a composed estimate reports the weakest.
5. [The model kinds](./models.md) — the concrete models you construct, and how to choose between them.

## Two quantities, one set of machinery

This section covers **timing** and **resources**, and they share more than they differ:

|  | timing | resources |
|---|---|---|
| where a number comes from | a *run* — cosim or an [XSI](../build/xsi.md) trace | a *report* — `csynth.xml` |
| what is recovered | a fit (`latency`, `ii`, a residual) | a measurement, attributed per module |
| keyed by | [platform](../platform/identity.md) = FPGA part + synthesis clock | the same |
| stored in | the same [record store](./modules.md) | the same |
| published by | the same [work → publish flow](../platform/workflow.md) | the same |

The asymmetry worth remembering is in the middle row. A timing number is *fit* — a model form with
coefficients recovered from a sweep. A resource number is *measured* — the report says 32 DSPs and
that is what it is. Predicting resources at an **unmeasured** point is a separate problem, and one the
[module keys](./modules.md) are designed to make cheap: two designs that induce the same module reuse
one measurement rather than paying for a second synthesis.

## Where the fit lives: custom vs shared infra

A fit is stored in one of two places, chosen by one knob:

- **Custom components** — your accelerator's own kernels. The fit is specific to your design, so it goes
  in a **project-local directory you pick** (`calib_dir`).
- **Shared-infra components** — reusable framework kernels (`MemRStream` / `MemWStream`, …). Their fit
  is a `(component, platform)` property, so it goes in a **git-tracked platform library** (`platform_dir`)
  and ships with the repo — reuse the component on a calibrated platform and inherit its timing with
  **no re-calibration**. The library is keyed by an FPGA-part identity (see [Platforms](../platform/identity.md))
  and populated through a [two-tier work → publish flow](../platform/workflow.md).

## Why a platform is keyed by part *and* clock

Both axes depend on the **synthesis** clock (`create_clock -period`), not just the part: HLS schedules
to meet it, so a different target period changes the schedule — and therefore both the cycle counts
and the logic that had to be replicated to hit it. A number measured at one period does not describe
the same hardware at another, which is why the [platform identity](../platform/identity.md) carries
both.

The *simulation* frequency is a different thing and changes nothing. Timing artifacts are stored in
**cycles** rather than seconds precisely so a re-deploy at a different sim frequency needs no refit.

## In this section

- [What a `CalibModel` is](./model.md) — the shape: `get_params` → `transform` → `predict_feat`, plus
  `confidence`, `fit`, and where a model's data lives.
- [The corpus](./corpus.md) — `corpus.csv`: one row per measurement, derived from each axis's raw tier
  rather than maintained.
- [`CalibDataFrame`](./dataframe.md) — the object implementing that format.
- [Confidence](./confidence.md) — `EXACT` / `INTERPOLATED` / `EXTRAPOLATED` / `UNCALIBRATED`, and why a
  composed estimate reports the weakest link.
- [The model kinds](./models.md) — lookup, prior, concat and the two regressions, and how to choose.
- [A worked example](./example.md) — the fit mechanics end to end: fit a `LinCalibModel`, score it,
  hold a point out; then a saturating curve with `InterpCalibModel`.
- [Module keys and the record store](./modules.md) — addressing a measurement by the module's
  *structure* rather than its parameter dict, and the one record envelope both axes use.

## The two axes

Everything above is shared. What differs is where a number comes from, and each axis documents its own
half:

- [Timing Models](../timing_model/) — declaring a model's form, then calibrating it: the direct sweep
  fit, the RTL-vs-pysim residual, the once-per-platform bus model.
- [Resource Models](../resource_model/) — predicting utilization: the model kinds a design attaches,
  `compose` over a hierarchy, and attributing a `csynth` report to the modules that caused it.

{: .note }
> **Pages that moved (2026-08-04).** *Fitting a timing model*, *Component residuals*, *The
> bus-transfer model* and *The mem-stream residual* are now under
> [Timing Models](../timing_model/); *Resource measurements* is under
> [Resource Models](../resource_model/resources.md). They were written when this was the timing
> calibration section; now that the base is genuinely shared, only the axis-agnostic pages belong
> here.

Everything here is *stored on a platform* — the identity it is keyed by, the directory layout, and the
commands that create and publish one are in [Platforms](../platform/).

## See also

- [Platforms](../platform/) — the target these fits are valid for, and where they live.
- [Timing Analysis Tools](../timing/) — the *measurement* side: extracting cycle counts and bus spans
  from a VCD / cosim run (where the datapoints come from).
- [`BuildConfig`](../build/corecomp.md) — the `platform` / `part` / `clk_freq` selector that names the
  platform a build synthesizes and calibrates for.
