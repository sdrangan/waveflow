---
title: Resource Models
parent: Guide
nav_order: 13
has_children: true
audience: python
api: [ResourceModel, get_rm, predict, compose, Confidence, ConfidenceLevel]
summary: "Predicting a design's resource utilization without synthesizing it, so a design-space exploration can price thousands of configurations from a handful of syntheses. Every module has a model — the default recalls measurements and needs no authoring; others derive from declared structure or regress from a corpus. Every prediction carries a confidence saying how much to believe it, and a composed estimate reports its weakest link rather than its average."
---
# Resource Models

## What a resource model is

[Resource Analysis Tools](../resource/) *measures* utilization, specifically counters of quantities of resource types, from a synthesis report. A **`ResourceModel`**  **predicts** the resource utilization based on a hardware models configuration.  For example, in an Xilinx / AMD FPGA flow, it can predict the number of FF, LUTs, or BRAM based on a hardware module's parameters such as the bitwidths or buffer sizes.  This predictio enables design-space exploration  to price thousands of configurations from just a handful of syntheses.

## What you write

To add a resource model to a `HwModule` class, you generally need to write the function: [`def get_rm(platform)`](./getrm.md) to provide a model for a specified [platform](../platform/).

Once `get_rm(platform)` function is specified, the `HwModule`'s functions [`predict`](./predict.md)  and [`compose`](./predict.md) can be used to predict the resource consumption of a `HwModule` and its hierarchy of modules contained by it.

**Every module has a model.** Returning `None` does not mean "unmodelled" — it means the default
[lookup](./lookup.md), a model whose parameters are the measurements themselves. That is the right
answer more often than it looks: a lookup assumes nothing about the shape of the cost function, so no
structural assumption can be wrong, and it is exact at every configuration it has seen.

## Model types

Three kinds, each with a page of its own. They differ in what they trade for an answer:

| kind | how it answers | costs you |
|---|---|---|
| [`LookupResourceModel`](./lookup.md) | recalls the measurement for exactly this configuration; refuses to interpolate | one synthesis per point you will ask about |
| [`VitisResourceModel`](./vitis.md) | prices countable structure by device rule, regresses the rest | a structure declaration, and a basis you have to choose |
| [`InterfaceResourceModel`](./interface.md) | recalls what a composite costs **beyond** its sub-modules | nothing — it is read from records the sweep already filed |

The first and third are the same machinery: an interface model **is** a lookup, keyed on the
composite's boundary instead of on a module key. What differs is the *quantity* it recalls, not the
method.

So the only real choice is the second row, and it is about **coverage, not sophistication**. Reach
past a lookup when the parameter space is too large to enumerate, and accept some bias in exchange for
answering points you never measured.

## What a confidence is

A bare number invites an exploration to optimize into a region nothing ever measured. So a prediction
is never just counters: every model returns a [`Confidence`](./predict.md#confidence) beside them.

| level            | means                                                                                                                                                          |
| ---------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `EXACT`        | the model's form reproduces every calibration point with zero residual — a checked claim, not an approximation                                                |
| `INTERPOLATED` | the query lies **inside** the region the model was calibrated over                                                                                        |
| `EXTRAPOLATED` | outside it. The level that matters most here, because what you cross on the way out is usually a *regime boundary* — and those move several counters at once |
| `UNCALIBRATED` | no fit backs this number                                                                                                                                       |

Two properties are worth knowing because they are what make the level trustworthy rather than
decorative:

**A composed estimate reports its weakest link, not its average.** If three modules are exact and one
is extrapolated, the total is `EXTRAPOLATED` — and it names which module, which is what you would go
and measure first. An estimate that averaged its confidences would read as fine while resting on the
one number nobody checked.

**A model that has not seen a configuration says so.** A [lookup](./lookup.md) asked about a point it
does not hold reports `UNCALIBRATED` rather than returning its nearest entry. That refusal is the
design, not a gap in it: a plausible wrong number is worse than an admitted absence, because only one
of the two gets investigated.

## In this section

Simplest first — each page has one job.

1. [What a `ResourceModel` is](./rm.md) — a [`CalibModel`](../calib/model.md) whose targets are the
   platform's counters: the methods, what counters are, and why the component-facing entries take a
   *component* rather than a feature vector.
2. [The lookup model](./lookup.md) — the simplest one, and the default: recall a measurement, refuse
   to interpolate. With a runnable example.
3. [Binding a model to a design](./getrm.md) — `get_rm`, and why it is a classmethod.
4. [`VitisResourceModel`](./vitis.md) — the model that *derives*: `resource_structure` field by
   field, the rule each primitive is priced by, and how to choose the basis LUT and FF are fitted on.
5. [The interface model](./interface.md) — a composite's own cost, keyed on its boundary: why that
   is the right key, and why it is a lookup rather than a fit.
6. [Predicting](./predict.md) — `predict`, `compose` over a hierarchy, and reading the confidence.
7. [Fitting](./fit.md) — corpus, basis, `load_or_fit`, and validating held-out.
8. [Resource measurements](./resources.md) — attributing a `csynth` report to the modules that caused
   it, the two traps that otherwise corrupt the numbers, and the `InspectSynthStep` build rung.

A fully worked instance, built and measured end to end, is
[the VecMult example](../../examples/vecmult/); the advanced case — a composite, with state and an
interface term — is [the block FIR](../../examples/firblock/resource_model.md).

## See also

- [Model calibration](../calib/) — the shared base these models are built on: the `CalibModel` shape,
  the corpus format, the confidence levels and the model kinds.
- [Resource Analysis Tools](../resource/) — where the measurements these models are built from come from.
- [Timing Models](../timing_model/) — the same role on the other axis.
