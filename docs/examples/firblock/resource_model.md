---
title: Resource models
parent: Block FIR (state + fixed point)
nav_order: 9
has_children: false
audience: python
api: [dsp_prior, bram_prior, fir_compute_basis, fir_compute_fitted, add_rm, add_rm_self, compose]
summary: "The four models this design declares, one counter at a time, and how they are installed. DSP and BRAM are binding decisions and get zero-parameter priors from the DSP48E1's geometry and from the ARRAY_PARTITION pragma; LUT and FF are genuinely estimated and get a fit over structural features chosen for meaning; the composite's own cost is a lookup on boundary structure. Installation is one method per class -- add_rm_self -- with three of the five modules needing none, and top.add_rm(platform) recursing the elaborated graph."
---

# Resource models

This page is what the design *asserts* about its own area: four counters, four models, and the code
that attaches them. What the measurements say about whether those assertions hold is
[The sweep and its results](./resource_fit.md) — kept separate deliberately, so a claim and its
evidence do not get read as one thing.

The general machinery is [Resource Models](../../guide/resource_model/); this is one design's use of it.

## One counter at a time

A design fits only if *every* counter fits, so area is never a single number. What differs between them
here is not difficulty but **kind** — whether the number is a decision the design already made, or a
consequence nobody wrote down:

| counter | kind | model | free parameters |
|---|---|---|---|
| DSP | a binding decision — the kernel says how many multiplies, the device says what one costs | [prior](#dsp-geometry-not-statistics) | **none** |
| BRAM | a binding decision — a pragma in this design's own source puts the arrays in registers | [prior](#bram-a-prior-that-asserts-zero) | **none** |
| FF | consequence — partitioned storage, pipeline registers | [fitted](#lut-and-ff-the-estimated-half) | yes |
| LUT | consequence — accumulate tree, address and mux logic | [fitted](#lut-and-ff-the-estimated-half) | yes |

Half of this design's counters need no fitting at all. That is the ratio worth aiming for: every
counter moved from the bottom half to the top is one that stops needing data and starts extrapolating.

## DSP: geometry, not statistics

Two facts settle it, and neither is statistical.

**The DSP48E1 is a 25×18 signed multiplier**, so one `samp_w × samp_w` multiply costs:

| `samp_w` | ≤ 8 | ≤ 18 | ≤ 25 |
|---|---|---|---|
| DSPs | **0.5** — two multiplies share one | 1 | **2** — one operand exceeds 18 bits, so the product splits |

**The kernel says how many multiplies it has** — `NTAP` for the serial body; `NTAP × LW` for the
unrolled one, where `LW = mem_dwidth // samp_w` (the unrolled body's own comment: *"LW independent
windows → LW*NTAP multipliers"*).

```python
def dsp_prior(f):
    n = n_multipliers(f["ntap"], f["samp_w"], f["mem_dwidth"], f["unroll_lane"])
    dsp = math.ceil(n * dsp_per_mult(f["samp_w"]))
    if not f["unroll_lane"] and dsp_per_mult(f["samp_w"]) < 1.0:
        dsp += SERIAL_PACK_CORRECTION          # see below
    return dsp
```

Both inputs come from things already written down — the device datasheet and the kernel source — which
is why this costs zero fitted parameters and holds outside any grid. It is
[exact at all 24 measured points](./resource_fit.md#dsp-the-prior-holds-exactly).

{: .note }
> One thing the geometry does **not** explain: a constant `+1` in the serial packed case. It is
> constant across every `NTAP`, so it reads as one multiply that failed to find a partner rather than a
> wrong law — kept as a named constant, `SERIAL_PACK_CORRECTION`, so it stays visibly unexplained
> instead of being absorbed into the formula and forgotten.

## BRAM: a prior that asserts zero

```python
def bram_prior(f):
    return 0
```

Not a default, and not "no BRAM was observed." The tap and history arrays carry an `ARRAY_PARTITION`
from their [`add_state`](./state.md) declaration, which maps their storage into LUTs and registers.
The prior *asserts* that consequence, so a future configuration that does spill into block RAM shows up
as a **prior failure** rather than passing unnoticed.

This is the cheapest kind of model to write and the easiest to skip. A module with no BRAM model would
predict nothing and be reported as unknown; a module asserting zero is making a falsifiable claim about
its own pragmas.

## LUT and FF: the estimated half

No closed form reaches these. They are fitted — but over **structural** features rather than the raw
parameters:

| feature | what it means |
|---|---|
| `n_mult` | multipliers instantiated |
| `store_bits` | taps + delay line, in bits. Partitioned, so it lands in registers. Realization-dependent: serial keeps `NTAP` entries, unrolled keeps `NTAP + LW - 1` |
| `acc_bits` | `2W + ceil(log2 NTAP)` — the width the [format algebra](./fixedpoint.md) derives |
| `mac_bits` | `n_mult × acc_bits` — pipeline register area, to first order |

```python
FittedResourceModel(targets=("lut", "ff"),
                    transform_fn=fir_compute_basis,     # params -> {feature name: value}
                    basis={"ff":  ["store_bits", "n_mult"],
                           "lut": ["n_mult", "store_bits", "mac_bits"]},
                    prior=fir_compute_prior())          # DSP/BRAM ride inside
```

`transform_fn` turns a **parameter row** into named features; `basis` picks, per counter, which of
them that counter regresses on. They are separate because one transform feeds both counters and the
two have different forms. The `prior=` is not a wrapper — one object predicts all four counters, each
from whichever half is honest for it.

{: .note }
> `transform_fn` takes parameters rather than a component, which is what guarantees the fit can be
> reproduced from a stored [corpus](../../guide/calib/corpus.md) — see
> [What a `CalibModel` is](../../guide/calib/model.md). The general form of this
> prior-plus-fit pairing is now [`ConcatCalibModel`](../../guide/calib/models.md#concatcalibmodel--one-model-per-target);
> `prior=` predates it and is scheduled for retirement.

**Features are chosen for meaning, not for fit.** `store_bits` is what partitioned storage physically
costs; it is also what lets a *single* model span both kernels, since its length carries the
realization difference. That choice has a measured price and is kept anyway — see
[the two choices that went against the fit](./resource_fit.md#lut-and-ff-what-the-fit-achieves).

## The composite's own cost

`FirBlock`'s own term — `m_axi` adapters, channel FIFOs, control block, DATAFLOW shell — is a lookup
keyed on **boundary structure**, not on parameters:

```python
table[boundary_signature(probe)] = counters      # one probe per measured mem_dwidth
```

That is not a preference; it is what the measurements say. The term was invariant across all 24 compute
configurations and moved only when the memory word width did — the evidence is in
[the interface term](../../guide/resource_model/predict.md#the-interface-term).

## Installing them

A model reaches the design by being declared **on the module**, as one method:

```python
class FirCompute(FreeRunMod):
    def add_rm_self(self, platform):
        samples = [(elaborate(FirCompute, {...}, name="fit"), m) for n, w, u, m in points()]
        self._resource_model = fir_compute_fitted(platform=platform).fit(samples)
```

Then one call attaches everything:

```python
top = elaborate(FirBlock, params)
top.add_rm(platform)            # recurses children-first; every module ends up with a model
est = compose(top)              # needs nothing but the graph
```

Only **two** of the five modules define `add_rm_self` at all. `FirCmdRx`, `MemRStream` and `MemWStream`
keep the inherited default — a lookup against the platform store — because each was measured once and
its area is a fact to recall rather than a function to fit. Expect that ratio: the authoring effort
concentrates in the few modules whose area actually moves.

{: .note }
> **Why on the class, and not installed from outside.** These were briefly attached by assigning onto
> the classes from `fir_block_resource.py`, to keep the design module free of calibration imports. It
> bought nothing — both methods import what they need inside the body, so `fir_block.py` gained no
> module-level dependency either way — and it cost the reader a level of indirection plus a call that
> had to happen before `add_rm` would work. Declared on the class, a design estimates as imported.

What stays in `fir_block_resource.py` is the model *content*: the priors, the feature transform, and
the fitted model's shape. Those are calibration concerns, and a design module has no reason to carry
them.

## See also

- [The sweep and its results](./resource_fit.md) — the measurements these models are checked against.
- [The model kinds](../../guide/resource_model/rm.md) — prior, fitted, lookup and interface in general.
- [The two kernels](./kernels.md) — the bodies the multiplier counts are read off.
- [Fixed point](./fixedpoint.md) — where `acc_bits` comes from.
