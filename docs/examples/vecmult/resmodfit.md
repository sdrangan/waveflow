---
title: How well it fits
parent: Vector multiply resource modeling
nav_order: 8
audience: python
api: [resource_structure, get_rm, VitisResourceModel, DesignStructure, LutFfBasis, MultGroup, MemArray, VitisDerived, dsp_count, bram_estimate, lutram_luts]
summary: "The model with both halves, checked against points held out of its own fit. DSP, BRAM and LUT are all exact at every one of the 16 measurements — LUT including the point where the buffer leaves block RAM, because a device rule prices the distributed RAM it became. FF is 1.7% in-regime and 3.5% at that corner, which is the one gap left and is reported rather than hidden. Then an appendix: why the obvious basis fails by 43%, and how the LUTRAM rule was predicted before it was measured."
---

# How well it fits

The [declaration](./vitis_resmod.md) already gave us DSP and BRAM. The [sweep](./sweep.md) has now
given LUT and FF their measurements. This page is the check: with both halves in place, how close is
the model at configurations it was **not** trained on?

## Asking it for a point

Elaborate a configuration, install the model and compose:

```python
top = elaborate(VecMult, {"dwid": 64, "vlen": 4096}, name="vec_mult")
top.add_rm(platform)          # once, on the top — it recurses the whole hierarchy
est = compose(top)
```

```text
total      {'lut': 1370, 'ff': 597, 'dsp': 4, 'bram': 4}
level      INTERPOLATED
weakest    [('vec_mult', 'VecMult')]
```

Measured at that configuration: `{'lut': 1370, 'ff': 599, 'dsp': 4, 'bram': 4}`.

Across the whole grid, holding each point out of the fit in turn:

| counter | error | free parameters | needed the sweep? |
|---|---|---|---|
| DSP | **exact**, 16/16 | 0 | no — [right before it ran](./vitis_resmod.md#before-any-sweep) |
| BRAM | **exact**, 16/16 | 0 | no — same |
| LUT | **exact**, 16/16 | 4 | yes |
| FF | **1.7 %** worst in-regime, 3.5 % at [the corner](#one-honest-limitation-and-it-is-now-ffs-alone) | 4 | yes |

The top two rows are the declaration being *confirmed* rather than fitted: 16 syntheses agreeing with
a formula that had zero free parameters is a stronger claim than a regression passing through them,
and it needed no held-out validation to be believed. The sweep bought the bottom two.

LUT reaching *all sixteen* takes one more thing than the fit — the point where the buffer leaves block
RAM is a different regime, and it is exact there because
[a device rule prices what the storage became](#lutram-luts) rather than the regression guessing at it.

`level` is the second thing to read, and it is `INTERPOLATED` rather than `EXACT` on purpose: a
composed confidence is the **weakest link**, and two of the four counters are regressions. It names
which one — *"vec_mult: INTERPOLATED; weakest target(s) ff"* — so the limitation is reported rather
than averaged away.

## If the error is bad, add a term — or find the rule

Two moves are available when a counter is not good enough, and they are not equally good:

1. **Add a basis term** and re-validate held out. Cheap, honest, and what
   [`LutFfBasis`](./vitis_resmod.md#add-a-term-and-check) is for — it is how FF got its third term.
2. **Find the device rule**, if the cost is something *countable* rather than something the tool
   decides. Strictly better where it applies: zero parameters, and it extrapolates.

LUT at the LUTRAM corner is the second move, and it is worth seeing why the first would have failed
there. One point cannot support a fitted regime term; the same point verified a **rule** the moment
that rule was written down, because [a rule can be predicted before it is measured](#lutram-luts).

---

## Under the hood {#under-the-hood}

Everything below here is *why* this works. None of it is needed to use it.

### The zero-parameter half

`MultGroup` and `MemArray` go straight to the
[device rules](../../guide/resource_model/vitis.md#the-device-rules) with **zero** fitted
parameters. Those rules are keyed on the **part**, because that is what they are properties of — a
DSP48E2 is 27×18, and a model written against a DSP48E1 is simply wrong on one.

{: .note }
> Before these rules existed, the DSP48E1 constants lived privately in `fir_block_resource.py` and the
> BRAM shape privately in `vecmult_corpus.py` — two copies of one part, with a third accruing in
> English on this page. Both examples now state only their structure, and `fir_block`'s DSP prior
> still reproduces its own 24 points exactly.

### Which terms to reach for {#structure-form-dictionary}

You write the basis yourself, but you are not guessing from nothing — hardware structures have known
growth forms, and this is the shortlist worth trying first:

| what is in the body | term to try |
|---|---|
| control at any size — handshakes, an FSM, a command decode | **constant** (the intercept, always present) |
| a datapath replicated per lane | **LW** |
| a counter or address register over N items | **log2(N)** |
| a reduction tree across lanes (sum, max) | **LW** area, `log2(LW)` depth |
| **any lane can reach any position** — variable-position mux, shifter, crossbar | **LW²**, **LW²·log2(LW)** |
| a multiply | none — `dsp_count` counts it |
| a partitioned array | none — `bram_estimate` / `lutram_luts` count it |

The last two rows are the important ones: if a cost is *countable*, it does not belong in the basis at
all. For `VecMult`'s three terms the fit comes out as:

```text
lut = 696.9 + 101.75·LW + 15.17·LW² +  0.73·LW²·log2(LW)
ff  = 516.6 - 165.09·LW + 70.21·LW² - 11.94·LW²·log2(LW)
```

{: .warning }
> **Two honest caveats on the fit.** LUT's coefficients are all positive and individually meaningful;
> **FF's alternate in sign**, so those basis terms are collinear for FF — it predicts well, but its
> coefficients must not be read one at a time. And the `LW²` cost is the price of offering a runtime
> length at all: a fixed-length kernel would drop that term and scale linearly. The model makes that
> trade **visible** rather than assuming it away.

#### Why the obvious features fail {#why-the-obvious-features-fail}

The natural first guess is that logic scales with datapath width: `c0 + c1·dwid + c2·log2(vlen)`.
Fitted on the same points, that reaches **43 % error on LUT and 52 % on FF**.

The measurements say why, before any fitting. LUT is **byte-identical across all four `vlen`** at each
width (964, 964, 964, 964 at `dwid=32`), so the length term is inert. And across width it grows far
faster than linearly — 964 → 1370 → 2622 → 6956, where linear predicts roughly 964 → 1930 → 3860.

In basis terms: the guess had a linear term and a `log2(vlen)` term and **missed `LW²`**. A design
decision — offering a runtime `n` — shows up as a cost curve.

So the [`LW²` term](./vitis_resmod.md#perlane) is the one that dominates `VecMult`'s LUT count,
and it is the whole difference between a model that predicts to 0.00 % and one that is 43 % off. That
is what the declaration bought: the price of a runtime length is *visible* in the model rather than
absorbed into a fitted constant that no longer means anything.

{: .note }
> **This cross-checks against a different design.** `fir_block` fits on per-lane datapath and storage
> terms and **no** `LW²`, because its kernels index statically — and the same method fits it too
> ([its resource models](../firblock/resource_model.md)). A basis that transferred unchanged between
> two designs this different would be the suspicious outcome, not the reassuring one.

### The corner: predicted, not absorbed {#the-corner-predicted-not-absorbed}

One point in the grid has no block RAM at all: `vlen=512, dwid=256`, where banks are 32 deep. HLS put
the buffer in LUTRAM, and the storage reappears in fabric — against the same lane count with the
buffer in block RAM, LUT 6956 → 7084 and FF 3618 → 3755.

The threshold lives in the device rule, so this is **predicted** rather than an unmodelled corner:

```python
>>> bram_estimate(n_banks=16, depth=32, elem_bits=16, part="xc7z020clg484-1")
BramEstimate(blocks=0, binding='lutram', entries_per_block=1024, blocks_per_element=1)
```

The rule reports the *binding*, not just a number — so a caller that needs a count and a caller that
needs a confidence get different things. It was **measured, not assumed**: bank depths 40, 48, 56, 60
and 63 all land in LUTRAM, and 64 lands in block RAM, so the boundary is 1008 bits per bank versus
1024.

{: .warning }
> One limit the rule states about itself: with `samp_w` fixed at 16, this corpus **cannot** tell
> "depth ≥ 64" apart from "bits ≥ 1024" — they coincide at every measured point. The rule is written
> in bits because 1024 is the round number, but confirming that needs a second element width. The band
> between the two observations is reported as `uncertain` rather than interpolated.

The lesson survives its own promotion: had this been a fitted term, the discontinuity would have been
**absorbed** into slightly wider error bars and never noticed. It was visible only because a formula is
allowed to be *wrong* in a way a regression is not.

#### And the storage it moved into is countable too {#lutram-luts}

Knowing the buffer *left* block RAM is only half an answer — it went somewhere. It went to distributed
RAM, and that is as countable as a block: a SLICEM LUT6 is a 64×1 RAM, so

```text
LUTs = banks × depth × elem_bits / 64
```

with **zero fitted parameters**, the same standing as `bram_estimate`. This was verified the honest way
round — predicted first, then synthesized:

| banks | depth | bits | predicted | measured |
|---|---|---|---|---|
| 2 | 32 | 1024 | +16 | **+16** |
| 8 | 16 | 2048 | +32 | **+32** |
| 8 | 32 | 4096 | +64 | **+64** |
| 16 | 16 | 4096 | +64 | **+64** |
| 16 | 32 | 8192 | +128 | **+128** |

Note there is **no per-bank ceiling**, unlike block RAM — and that is measured rather than assumed. It
is the discriminating prediction: rounding each bank up to whole LUTs per bit would cost 256 at the
last row, where the truth is 128. A LUT6 splits into two 32×1 RAMs, so shallow banks share one instead
of each claiming their own.

The consequence is that **the corner no longer has to be excluded from the fit.** `lutram_luts` is
subtracted from the measurement before fitting and added back when predicting, so the regression sees
only the fabric it is responsible for, and the regime is carried by a rule rather than by a coefficient
inferred from one point. LUT is now exact at **all 16** points, the corner included.

This is also what `VecMultResourceModel` exists for. Its `corpus()` keeps a row only when
`bram_estimate` says that row's declared banks bound to block RAM — the same function the derived half
uses to *predict* the counter — so the training regime cannot drift away from the threshold that
defines it, and a future grid point on the LUTRAM side is excluded automatically.

### Where the composed number comes from

```text
DesignStructure           what VecMult declares it contains
  ├─ VitisDerived         dsp, bram, uram, srl   — device rules, 0 free parameters
  └─ LinCalibModel × 2    lut, ff                — regressed on terms derived from the same declaration
       ↓
  VitisResourceModel = ConcatCalibModel(the above)
       ↓
  VecMult.get_rm(platform)  →  top.add_rm(platform)  →  compose(top)  →  ResourceEstimate
```

[`ConcatCalibModel`](../../guide/calib/models.md#concatcalibmodel--one-model-per-target) is what lets
each counter come from whichever model is honest for it while one object presents all four; the
composition, not this example, owns the arithmetic that combines them.

{: .note }
> **What the corpus records is the declaration, not the terms.** A row holds `mult0_count`,
> `mem0_banks`, `xbar0_lanes` — the flattened structure — and the basis terms are derived from it at
> fit time. The cost of a crossbar in LUTs is a modelling claim that will be revised; storing lane
> counts means a revision re-derives from measurements already on disk rather than stranding them.
> See [the corpus](../../guide/calib/corpus.md#raw-not-derived).

### The integration term, and why it is negative here {#integration-term}

A synthesis reports a figure per task **and** a figure for the whole design, and they are not the same
number. The difference is the **integration term**:

```text
integration  =  top  −  Σ(modules)
```

For most designs it is substantial and positive — it holds the `m_axi` adapters, the inter-task FIFOs,
the AXI-Lite control block and the DATAFLOW shell. On [`fir_block`](../firblock/resource_model.md),
which has four tasks, it is **1984 LUT: 29 % of the design**, the second-largest contributor after the
compute.

VecMult is the rare opposite. It has **one** task and no adapters, so there is almost nothing at the
boundary for the term to contain — and what remains is negative:

```text
top          lut 6956          the whole design
module_sum   lut 6958          VecMult's own cost
integration  lut   -2          Vitis flattening the single instance
```

Identical at all 16 points including across all four port widths, so it is flattening slack rather
than anything that scales with the interface.

{: .note }
> **Nothing clamps it at zero.** A negative own-cost is the signal that additivity is leaking across a
> module boundary, and hiding it would hide exactly what whole-design synthesis exists to catch. It is
> carried by an [`InterfaceResourceModel`](../../guide/resource_model/predict.md) keyed on the boundary
> signature — the same class and the same lookup a multi-task composite uses, so the single-task case
> is not a special path.

The practical consequence is that the model is fitted on **module** figures with the integration term
added back, rather than fitted on design totals with the −2 silently absorbed into its coefficients.
Same prediction either way here; only one of them stays right when a second task appears.

### One honest limitation, and it is now FF's alone

LUT at the corner is exact. **FF is not** — it under-predicts by 3.5 %, and that is deliberate rather
than unfinished.

The obvious move would be to give FF the same treatment as LUT. The measurements refuse it: FF's cost
at the corner is **flat in depth** — 140 at 8192 bits and 140 at 4096 — so it is not storage at all. It
is per-lane registering, and across three lane counts (8.5 / 32.8 / 140 at `LW` = 2 / 8 / 16) three
points did not determine a form. Inventing one would be exactly the error
[the corner section](#the-corner-predicted-not-absorbed) argues against, with the added insult of
having a working example of the right way to do it on the same page.

So FF alone still narrows its training rows, and the gap is pinned with a bound rather than left as a
footnote — under-prediction being the one direction a resource estimate must not drift:

```python
assert (measured["ff"] - total["ff"]) / measured["ff"] < 0.05, \
    "corner FF under-prediction grew beyond 5% — the regime term is now worth determining"
```

What would settle it is more lane counts in the LUTRAM regime, which also needs the in-BRAM baseline
extended past `LW=16` — at `LW=32` the baseline is itself an extrapolation, and differencing against it
produces nonsense (−936 LUT) rather than evidence.

{: .note }
> This gap was invisible until the model was **composed**. Checking the device rules directly — which
> the tests already did — showed four green counters. Only running the installed model end to end
> revealed that the fitted half disagrees with the prior half about what happens at the corner.
> Validating the formulas is not the same as validating the model.

### And before writing a model at all

Ask how many configurations the exploration will actually visit. If `vecmult` is only ever built at two
widths, two syntheses answer every question exactly and a
[lookup](../../guide/resource_model/lookup.md) is the whole model. On `fir_block` that was the right
answer for three of its four modules. **Fitting is the exception, not the default.**

## See also

- [The resource model](./vitis_resmod.md) — the class, the six declaration terms, and what they
  already predict before any measurement.
- [The sweep](./sweep.md) — where these 16 points came from.
- [`VitisResourceModel`](../../guide/resource_model/vitis.md) — the same vocabulary as reference, plus
  the device rules in full.
- [Resource models](../../guide/resource_model/) — the concepts, of which this page is the instance.
- [Block FIR resource models](../firblock/resource_model.md) — the same method on a composite design
  with different structures.
