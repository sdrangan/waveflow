---
title: The model kinds
parent: Resource Models
nav_order: 1
has_children: false
audience: python
api: [ResourceModel, LookupResourceModel, PriorResourceModel, FittedResourceModel, InterfaceResourceModel]
summary: "Four kinds, sized to what the measurements support: a lookup for modules that do not vary (three of four on the reference design), a prior for the counters that follow device geometry, a fit for LUT and FF, and an interface model for a composite's own cost. Features are read off the elaborated component — parameters or structure — and nothing is passed down from a parent."
---

# The model kinds

Every model answers the same question — *what does this module cost, excluding its children?* — and
implements the same two methods:

```python
class ResourceModel:
    def features(self, comp) -> dict:  ...   # read off the elaborated component
    def predict_own(self, comp) -> dict:     # {lut, ff, dsp, bram, ...}
    def confidence_own(self, comp) -> Confidence
    def fit(self, samples) -> ResourceModel  # no-op unless there are free parameters
```

{: .note }
> **Features are not only parameters, and nothing is passed down.** A leaf model typically wants
> resolved `HwParam` values; a composite's model wants *structure* — port and channel counts and
> widths. Both come off the instance, because [elaboration](../flows/parametrization.md) already
> resolved every child's parameters from its parent. A model reading its own features cannot drift
> from the design that was synthesized.

## Lookup

For a module that does not vary with the knobs being explored — which, measured on the reference
design, was three of the four.

```python
LookupResourceModel(store=module_store)     # or table={key: counters}
```

Keyed by [module key](../calib/modules.md), so it returns the measurement for *this exact
configuration*. It has no free parameters, and it **refuses to interpolate**:

```python
m.confidence_own(comp).level      # EXACT if measured, UNCALIBRATED if not
```

{: .warning }
> A lookup that quietly returned its nearest entry would be the precise mechanism by which an
> exploration walks into a region nothing measured — and it would look like a working model the whole
> way. `UNCALIBRATED` is the honest answer, and it is actionable: it tells you which synthesis to
> spend next.

## Prior

For counters that are **binding decisions** rather than estimates. A formula, no fitting.

```python
PriorResourceModel(formulas={"dsp": dsp_prior, "bram": bram_prior})
```

The FIR's DSP prior is `n_mult × dsp_per_mult(samp_w)`, from two facts:

- **DSP48E1 geometry** (25×18 signed): `samp_w ≤ 8` → 0.5 DSP per multiply (two share one);
  `≤ 18` → 1; `≤ 25` → 2 (one operand exceeds the 18-bit port, so the product splits).
- **the kernel's multiplier count**: `NTAP` serial, `NTAP × LW` unrolled.

That reproduces all 24 measured points **exactly with zero fitted parameters** — a much stronger claim
than any regression, and only worth making because it is checked at every point.

{: .note }
> **A worked reason to encode physics instead of curve-fitting.** The unrolled kernel measures
> `2·NTAP` DSPs at *every* sample width, which looks like a plateau worth hard-coding. It is not: lane
> count *falls* as width rises (`LW = mem_dwidth // samp_w`) while DSP-per-multiply *rises*, and over
> this device's step boundaries they cancel exactly. Hard-coding `2·NTAP` would be indistinguishable
> on that grid and **wrong the moment `mem_dwidth` changes** — at `mem_dwidth=64, samp_w=16` the
> product is 4.

A prior can also *assert* a zero. The FIR's BRAM prior returns `0` because the arrays carry
`ARRAY_PARTITION` and land in registers — so a future configuration that does spill into block RAM
shows up as a **prior failure** rather than passing unnoticed.

## Fitted

For LUT and FF: partitioned storage, pipeline registers, the accumulate tree, address and mux logic.
No closed form reaches them.

```python
FittedResourceModel(counters=("lut", "ff"),
                    basis={"ff": ["store_bits", "n_mult"],
                           "lut": ["n_mult", "store_bits", "mac_bits"]},
                    feature_fn=compute_features)
```

One [`LinCalibModel`](../calib/models.md) per counter, because the counters have different forms and a
single multi-target fit would be wrong. Confidence comes from the underlying model's retained
[`FitSummary`](../calib/modules.md), so an out-of-range query is reported as extrapolation exactly as
it is for a timing fit.

**Features are chosen for meaning, not for fit.** `store_bits` is the tap array plus the delay line in
bits — physically what partitioned storage costs, and correlated 0.985 with FF across the grid. Its
length is realization-dependent (serial keeps `NTAP` entries, unrolled keeps `NTAP + LW - 1`), which is
one of the two features that let a *single* model span both kernels.

{: .note }
> That choice has a price, and it is the right price. The physically-correct storage feature fits FF
> marginally *worse* than a sloppier one on this grid. It is kept, because a feature chosen for meaning
> extrapolates and a feature chosen for fit does not — and the grid is 24 points, well inside the range
> where the difference is noise.

## Interface

A composite's **own** cost — adapters, channel FIFOs, control block, DATAFLOW shell.

```python
InterfaceResourceModel(table={boundary_signature(top): counters})
```

Keyed on `boundary_signature()` — the shape of the interface graph, external ports and internal
channels, with names and order dropped as context. See
[Composing a design estimate](./composition.md#the-interface-term) for why that is the right key and
what the evidence for it is.

## Choosing

| the module… | use |
|---|---|
| does not vary over the knobs you explore | lookup |
| has a counter set by a *binding decision* (DSP, BRAM) | prior |
| has LUT/FF that move with its parameters | fitted, over structural features |
| is the composite itself | interface |

Note the first row covers most modules in a real design, and the last is one per design. The fitting
work concentrates on the few modules that actually move.

## See also

- [Composing a design estimate](./composition.md) — putting them together.
- [Module keys and the record store](../calib/modules.md) — what a lookup looks up.
