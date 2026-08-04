---
title: Resource Models
parent: Guide
nav_order: 13
has_children: true
audience: python
api: [ResourceModel, VitisResourceModel, DesignStructure, compose, get_rm, resource_structure]
summary: "Predicting a design's resource utilization without synthesizing it. On an FPGA the split is not a judgement call: hard primitives are allocated and therefore countable from declared structure, while fabric is what everything else decomposes into and has to be fitted. A design declares what it contains once; the device rules price the countable half exactly and supply the basis for the rest. Every prediction carries how much to believe it, and a composed estimate is only as good as its weakest link."
---

# Resource Models

[Resource analysis](../resource/) *measures* utilization from a synthesis report. This section
*predicts* it, so a design-space exploration can price thousands of configurations from a handful of
syntheses.

## The one split everything follows from

```text
hard primitives are ALLOCATED     ->  countable    ->  derived from structure, 0 parameters
soft fabric is what the rest
  DECOMPOSES INTO                 ->  not countable ->  fitted from measurements
```

A DSP or a block RAM is a thing the tool *assigns*: you can count how many your design needs and look
up what the device charges. LUTs and flip-flops are what everything else turns into, and how much
depends on how the tool shares, retimes and packs — there is no table to look it up in.

That is not a per-design decision. On a Vitis target it is a property of the fabric, so
[`VitisResourceModel`](./vitis.md) encodes it once rather than every design re-deriving it.

## What you supply

Two things, and neither is device knowledge:

| you write | answers |
|---|---|
| [`get_rm(platform)`](./getrm.md) on the class | *which model prices this design?* — return `None` for the default lookup |
| [`resource_structure()`](./vitis.md#what-you-declare) on the module | *what does it contain?* — only needed by a model that **derives** |

Everything else follows. The declared multipliers and arrays are priced exactly by the
[device rules](./vitis.md#the-device-rules); the declared fabric structures become the
[basis](./fit.md) for the regression. There is deliberately no
second place to state the basis, which is what makes *"a bad held-out error means a missing
structure"* actionable rather than advisory.

## Most modules need no model at all

The instinct is that per-module modelling means writing a model per module. Measured on
`examples/fir_block` across a 24-point sweep, it does not:

| module | distinct configurations | what it needs |
|---|---|---|
| `MemRStream` | **1** | a lookup |
| `MemWStream` | **1** | a lookup |
| `FirCmdRx` | 4 | a 4-entry lookup |
| `FirCompute` | 24 | the only one needing a fit |

None of those needed a model that *generalizes*, because each was cheap to cover exhaustively —
measure every configuration it is ever asked about and a [lookup](./lookup.md) answers exactly, with
no assumption about shape that could be wrong. `get_rm` returning `None` gets you one, which is why
**a structural model is the exception, not the default**.

The choice is about **coverage, not complexity**: you reach for a structural model when the parameter
space is too large to enumerate, and you accept some bias in exchange.

## Every prediction says how much to believe it

A bare number invites an exploration to optimize into a region nothing ever measured. So every model
returns a [`Confidence`](./predict.md#confidence) alongside its counters, and a composed estimate
reports its **weakest link** — including which modules sit at it, which is what you would recalibrate
first.

A [lookup](./lookup.md) that has not seen a configuration reports `UNCALIBRATED` rather than its nearest entry. That
refusal is the design, not a gap in it.

## In this section

Simplest first — each page has one job.

1. [What a `ResourceModel` is](./rm.md) — a [`CalibModel`](../calib/model.md) whose targets are the
   platform's counters: the methods, what counters are, and why the component-facing entries take a
   *component* rather than a feature vector.
2. [The lookup model](./lookup.md) — the simplest one, and the default: recall a measurement, refuse
   to interpolate. With a runnable example.
3. [Binding a model to a design](./getrm.md) — `get_rm`, and why it is a classmethod.
4. [`VitisResourceModel`](./vitis.md) — the model that *derives*: `resource_structure`, the structures
   you can declare, and the rule each primitive is priced by.
5. [Predicting](./predict.md) — `predict`, `compose` over a hierarchy, and reading the confidence.
6. [Fitting](./fit.md) — corpus, basis, `load_or_fit`, and validating held-out.
7. [Resource measurements](./resources.md) — attributing a `csynth` report to the modules that caused
   it, the two traps that otherwise corrupt the numbers, and the `InspectSynthStep` build rung.

A fully worked instance, built and measured end to end, is
[the VecMult example](../../examples/vecmult/); the advanced case — a composite, with state and an
interface term — is [the block FIR](../../examples/firblock/resource_model.md).

## See also

- [Model calibration](../calib/) — the shared base these models are built on: the `CalibModel` shape,
  the corpus format, the confidence levels and the model kinds.
- [Resource analysis](../resource/) — where the measurements these models are built from come from.
- [Model calibration](../calib/) — storing measurements per module, and the confidence machinery.
- [Timing Models](../timing_model/) — the same role on the other axis.
