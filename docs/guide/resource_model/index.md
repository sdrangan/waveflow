---
title: Resource Models
parent: Guide
nav_order: 13.6
has_children: true
audience: python
api: [ResourceModel, LookupResourceModel, PriorResourceModel, FittedResourceModel, InterfaceResourceModel, compose]
summary: "Predicting a design's area without synthesizing it. One composition rule applied recursively — a module's own cost plus the sum of its children — where a composite's own cost is the adapters, FIFOs and control block it adds beyond them. Most modules need a table rather than a model; DSP and BRAM follow known physics and are encoded, not learned; only LUT and FF are fitted. Every prediction carries how much to believe it, and a composed estimate is only as good as its weakest link."
---

# Resource Models

[Resource analysis](../resource/) *measures* area from a synthesis report. This section *predicts* it,
so a design-space exploration can price thousands of configurations from a handful of syntheses.

## The composition rule

One line, applied recursively:

```text
predict(comp)  =  comp's OWN model  +  Σ predict(child)
```

A **leaf's** own cost is its whole cost. A **composite's** own cost is what it adds *beyond* its
children — the `m_axi` adapters, the inter-task FIFOs, the AXI-Lite control block, the DATAFLOW shell.

That is not a special third term bolted onto a per-module sum: it is the same rule one level up, and
it is exactly what a synthesis report measures as `top row − Σ task rows`
([Composite kernels](../resource/composite.md)). Definition and measurement coincide with nothing left
over, which is what makes the model checkable against a whole-design run.

## Most modules need a table, not a model

The instinct is that per-module modelling means writing a model per module. Measured on
`examples/fir_block` across a 24-point sweep, it does not:

| module | distinct configurations | what it needs |
|---|---|---|
| `MemRStream` | **1** | a lookup |
| `MemWStream` | **1** | a lookup |
| `FirCmdRx` | 4 | a 4-entry lookup |
| `FirCompute` | 24 | the only thing needing a fit |
| the composite's own term | 1 per boundary | a lookup keyed on structure |

Modules that do not vary with the knobs being explored have nothing to fit. So the [model
kinds](./models.md) are deliberately lopsided, and `fit()` is a no-op on most of them **by
construction** rather than by an exclusion flag.

## Encode the physics, learn the remainder

Not every counter is the same kind of quantity.

**DSP and BRAM are binding decisions** — HLS assigns each multiply to a DSP or to logic, each array to
BRAM or registers, and *reports* what it chose. Those follow the device's geometry, so they are
encoded as a [prior](./models.md#prior) and reproduce all 24 measured points **exactly, with zero
fitted parameters**.

**LUT and FF are the estimate** — partitioned storage, pipeline registers, the accumulate tree, address
and mux logic. No closed form reaches them, so they are [fitted](./models.md#fitted) from
physically-motivated features. Held out, they land around 7–10% mean error, and that is the honest
limit of the approach rather than a number to tune away.

## Every prediction says how much to believe it

A bare number invites an exploration to optimize into a region nothing ever measured. So every model
returns a [`Confidence`](../calib/modules.md) alongside its counters, and a composed estimate reports
its **weakest link** — including which modules sit at it, which is what you would recalibrate first.

A lookup that has not seen a configuration reports `UNCALIBRATED` rather than its nearest entry. That
refusal is the design, not a gap in it.

## In this section

- [The model kinds](./models.md) — lookup, prior, fitted, interface: what each is for and why the set
  is shaped the way it is.
- [Composing a design estimate](./composition.md) — `compose()` over the elaborated graph, the
  interface term, and weakest-link confidence.
- [Validating a model](./validation.md) — held-out testing against design totals, why decision
  fidelity is the claim worth making, and how a headline number can flatter a model.
- [Modelling your own design](./workflow.md) — the end-to-end order: capture every synthesis, sweep,
  see what actually moved, choose a kind per module, validate, publish.

A fully worked instance of all of it, with real numbers, is the
[block FIR's resource modelling](../../examples/firblock/resources.md).

## See also

- [Resource analysis](../resource/) — where the measurements these models are built from come from.
- [Model calibration](../calib/) — storing measurements per module, and the confidence machinery.
- [Timing Models](../timing_model/) — the same role on the other axis.
