---
title: Resource models
parent: Vector Multiply (resource modelling)
nav_order: 5
audience: python
api: [dsp_count, bram_estimate, device_for]
summary: "What VecMult asserts about its own cost. DSP and BRAM come from device rules with zero fitted parameters — you supply the structural counts, the library supplies the silicon. LUT and FF are fitted, on a basis taken from a structure-to-form dictionary rather than invented. The dividing line is not arbitrary: hard primitives are countable, soft fabric is not."
---

# Resource models

The [sweep](./sweep.md) produced 16 measured points. This page is what the design *asserts* about
them: which counters follow from structure, which have to be learned, and where the features for the
learned ones come from.

The headline is that **four counters need two different kinds of model**, and the first instinct —
one regression per counter — is wrong on half of them.

## The three laws

| counter | model | free parameters | accuracy |
|---|---|---|---|
| DSP | `dsp_count(n_mult=LW, operand_bits=16)` | **0** | exact, 16/16 |
| BRAM | `bram_estimate(n_banks=LW, depth=vlen/LW, elem_bits=16)` | **0** | exact, 16/16 |
| LUT | `c0 + c1·LW + c2·LW² + c3·LW²log2(LW)` | 4 | 0.00% held-out |
| FF | same basis | 4 | 1.73% held-out |

## What you supply, and what the library supplies

DSP and BRAM are **not** reasoned about on this page, and that is the point. They are two calls into
[`waveflow.calib.device_rules`](../../guide/resource_model/rm.md), and the division of labour is
strict:

| you supply — structural facts about *your* design | the library supplies — facts about *the silicon* |
|---|---|
| how many multipliers, and how wide their operands are | that a DSP48E1 multiplier is 25×18, that two ≤8-bit multiplies pack into one, that a >18-bit operand splits it in two |
| how many memory banks, how deep, of what element | that a block has legal port shapes (16-bit elements use the ×18 shape → 1024 entries, not 18432/16), that each bank rounds **up** independently, and that below 1024 bits a bank goes to LUTRAM instead |

So the whole DSP model for this design is one sentence: **one multiply per lane, 16-bit operands.**

```python
from waveflow.calib.device_rules import bram_estimate, dsp_count

def dsp_prior(vlen, dwid):
    return dsp_count(lane_width(dwid), SAMP_W, PART)

def bram_prior(vlen, dwid):
    lw = lane_width(dwid)
    return bram_estimate(lw, vlen // lw, SAMP_W, PART).blocks
```

Rules are keyed on the **part** because that is what they are properties of — a DSP48E2 is 27×18, and
a model written against a DSP48E1 is simply wrong on one. This is the same reasoning that puts the
counter vocabulary on the [platform](../../guide/platform/identity.md) rather than in a global.

{: .note }
> Before these rules existed, the DSP48E1 constants lived privately in `fir_block_resource.py` and the
> BRAM shape privately in `vecmult_corpus.py` — two copies of one part, with a third accruing in
> English on this page. Both examples now state only their structure, and `fir_block`'s DSP prior
> still reproduces its own 24 points exactly.

## Choosing fitted features: the structure→form dictionary {#structure-form-dictionary}

LUT and FF have no device rule, and the reason is worth stating because it explains the whole split:

> **Hard primitives are countable. Soft fabric is not.**
> A DSP or a block RAM is *allocated* — you can count what your design needs and look up what the
> device charges. LUTs and flip-flops are what everything else *decomposes into*, and how much a
> given structure decomposes into depends on how the tool shares, retimes and packs. There is no
> table to look it up in.

But the *shape* is still not arbitrary. A design is assembled from a small vocabulary of structures,
each with a known growth form. You do not invent basis functions; you look up the structures you know
are in your design, because you wrote it.

| structure you can point at | form | countable? |
|---|---|---|
| control at any size — handshakes, an FSM, a command decode | **constant** | fit |
| a datapath replicated per lane | **LW** | fit |
| a counter or address register over N items | **log2(N)** | fit |
| a comparator or enable per lane against a runtime bound | **LW** | fit |
| a reduction tree across lanes (sum, max) | **LW** area, `log2(LW)` depth | fit |
| **any lane can reach any position** — variable-position mux, shifter, crossbar | **LW²**, **LW²·log2(LW)** | fit |
| a multiply | — | **`dsp_count`** |
| a partitioned array | — | **`bram_estimate`** |

The method is four steps:

1. **List the structures in your body.** You wrote it; this is recall, not analysis.
2. **Count the ones with rules**, and take the *forms* of the rest as your basis.
3. **Fit only the coefficients**, and validate **held out**.
4. **If held-out error is bad, you missed a structure.** Go back to step 1.

Step 4 is the discipline that makes this a method rather than curve fitting. A bad fit is a *modelling*
failure, not a cue to add another polynomial term.

### Applying it to VecMult

Reading `vec_mult_task.h` and listing what is physically there:

| in the body | dictionary row | contributes |
|---|---|---|
| command decode, stream handshakes, two loop FSMs | control | constant |
| `LW` multipliers, one per lane | multiply | **`dsp_count`** — not fitted |
| `if (j < nlane)` — one test per lane | per-lane comparator | `LW` |
| `read/write_stream_lane(..., nlane)` — a **runtime** count of lanes at runtime positions | **crossbar** | `LW²`, `LW²·log2(LW)` |
| `buf` partitioned into `LW` banks | partitioned array | **`bram_estimate`** — not fitted |

Which gives, with nothing invented:

```text
lut = 696.9 + 101.75·LW + 15.17·LW² +  0.73·LW²·log2(LW)
ff  = 516.6 - 165.09·LW + 70.21·LW² - 11.94·LW²·log2(LW)
```

Leave-one-out over the 15 in-BRAM points: **LUT 0.00%**, FF 0.66% mean / 1.73% worst.

### Why the obvious features fail

The natural first guess is that logic scales with datapath width: `c0 + c1·dwid + c2·log2(vlen)`.
Fitted on the same points, that reaches **43% error on LUT and 52% on FF**.

The measurements say why, before any fitting. LUT is **byte-identical across all four `vlen`** at each
width (964, 964, 964, 964 at `dwid=32`), so the length term is inert. And across width it grows far
faster than linearly — 964 → 1370 → 2622 → 6956, where linear predicts roughly 964 → 1930 → 3860.

In dictionary terms: the guess assumed rows 2 and 3 and **missed the crossbar**. The crossbar is there
because `n` is a **runtime** length, so the final beat carries a variable number of lanes at variable
positions. A design decision shows up as a cost curve.

{: .note }
> **The dictionary cross-checks against a different design.** `fir_block` fits LUT and FF on
> `n_mult`, `store_bits` and `mac_bits` — a per-lane datapath and storage, no crossbar — because its
> kernels index statically. Same method, different structures, different basis, and it works there
> too ([its resource models](../firblock/resource_model.md)). A basis that transferred unchanged
> between two designs this different would be the suspicious outcome, not the reassuring one.

{: .warning }
> **Two honest caveats on the fit.** LUT's coefficients are all positive and individually meaningful;
> **FF's alternate in sign**, so those basis terms are collinear for FF — it predicts well, but its
> coefficients must not be read one at a time. And the crossbar cost is the price of offering a
> runtime length at all: a fixed-length kernel would drop that row entirely and scale linearly. The
> model makes that trade **visible** rather than assuming it away.

## The corner: predicted, not absorbed

One point in the grid has no block RAM at all: `vlen=512, dwid=256`, where banks are 32 deep. HLS put
the buffer in LUTRAM, and the storage reappears in fabric — against the same lane count with the
buffer in block RAM, LUT 6956 → 7084 and FF 3618 → 3755.

This began as the prior's one miss. It is now **predicted**, because the threshold moved into the
device rule where it belongs:

```python
>>> bram_estimate(n_banks=16, depth=32, elem_bits=16, part="xc7z020clg484-1")
BramEstimate(blocks=0, binding='lutram', entries_per_block=1024, blocks_per_element=1)
```

The rule reports the *binding*, not just a number — so a caller that needs a count and a caller that
needs a confidence get different things. That distinction is what lets an under-determined band stay
under-determined instead of becoming a confident wrong answer.

The threshold was **measured, not assumed**: bank depths 40, 48, 56, 60 and 63 all land in LUTRAM,
and 64 lands in block RAM. The boundary is 1008 bits per bank versus 1024.

{: .warning }
> One limit the rule states about itself: with `samp_w` fixed at 16, this corpus **cannot** tell
> "depth ≥ 64" apart from "bits ≥ 1024" — they coincide at every measured point. The rule is written
> in bits because 1024 is the round number, but confirming that needs a second element width. The
> band between the two observations is reported as `uncertain` rather than interpolated.

And the lesson survives its own promotion. Had this been a fitted term, the discontinuity would have
been **absorbed** into slightly wider error bars and never noticed. It was visible only because a
formula is allowed to be *wrong* in a way a regression is not.

## Installing the model

A model that is never installed predicts nothing. The chain from the device rules to a usable
estimate has one more link, and it is declared **on the module**:

```python
class VecMult(FreeRunMod):

    def add_rm_self(self, platform) -> None:
        samples = [(elaborate(VecMult, {"dwid": d, "vlen": v}, name="fit"), m)
                   for v, d, m in in_bram_points()]
        self._resource_model = vec_mult_fitted(platform=platform).fit(samples)
```

Declared on the class rather than assigned from `vecmult_resource.py`, so `top.add_rm(platform)`
works on the design *as imported* — and a reader looking for a module's model finds it on the module,
not in a registry that has to be kept in step.

Note what the fit is trained on: `in_bram_points()`, **not** the whole grid. The LUTRAM corner is a
different regime, and asking one line to span a discontinuity does not make it better there — it
makes it worse everywhere else.

### The whole chain

```text
device_rules.dsp_count / bram_estimate        the silicon
   └─ dsp_prior(features) / bram_prior(features)      your structural counts
        └─ PriorResourceModel(formulas={"dsp": …, "bram": …})
             └─ ConcatCalibModel(VitisDerived(dsp/bram), LinCalibModel(lut), LinCalibModel(ff))
                  └─ VecMult.add_rm_self(platform)     →  self._resource_model
                       └─ top.add_rm(platform)  →  compose(top)  →  ResourceEstimate
```

One detail is easy to get wrong: `PriorResourceModel.formulas` takes
`callable(features: dict) -> int`, where *features* is the **resolved `HwParam` set** that
[elaboration](../../guide/comp_codegen/elaborate.md) produced. Nothing is threaded down from a
parent, because each model reads its own features off the instance it is attached to.

### Composing an estimate

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

Measured at that configuration: `{'lut': 1370, 'ff': 599, 'dsp': 4, 'bram': 4}`. Across the whole
corpus, DSP and BRAM are exact at all 16 points and LUT is exact at all 15 in-BRAM points.

### Why the verdict is not `EXACT`

Two of the four counters have zero free parameters and reproduce every measurement. The composed
estimate still reports **`INTERPOLATED`**, because a composed confidence is the **weakest link** — and
LUT and FF are regressions.

That is the machinery refusing to overclaim. An estimate reporting `EXACT` while half of it is a fit
would be the most misleading thing this layer could do, so it is asserted directly:

```python
def test_composed_confidence_is_not_exact():
    assert _composed(4096, 64).level is not ConfidenceLevel.EXACT
```

The per-module confidence carries the reasons, not just the level — for this module,
*"lut: form reproduces all 15 calibration points exactly (4 free parameters); ff: inside the
calibrated region; worst residual on the corpus was 1.3%"*.

### One honest limitation, visible only from here

At the LUTRAM corner the composed estimate predicts **6956 LUT against 7084 measured** — it
**under-predicts by 1.8%**.

The BRAM prior is right there: it returns 0 because the bank is too small for a block. But the LUT/FF
fit was trained on in-BRAM points and has no term for a buffer that *became registers*, so it misses
the fabric that storage moved into.

Under-prediction is the one direction a resource estimate must not drift — it turns "does not fit"
into "fits" — so this is pinned with a bound rather than left as a footnote:

```python
assert (measured["lut"] - total["lut"]) / measured["lut"] < 0.05, \
    "corner under-prediction grew beyond 5% — the regime term is now worth adding"
```

A complete model would add a LUTRAM regime term to the fitted half, mirroring the branch the prior
already has. It has not been added because one corner is one data point, and
[fitting a regime from one point](#the-corner-predicted-not-absorbed) is the error this whole page
argues against.

{: .note }
> This gap was invisible until the model was **composed**. Checking `dsp_prior` and `bram_prior`
> directly — which the tests already did — showed four green counters. Only running the installed
> model end to end revealed that the fitted half disagrees with the prior half about what happens at
> the corner. Validating the formulas is not the same as validating the model.

## Why encode rather than fit, restated

Half of this design's counters follow from structure and device geometry, and encoding them is
strictly better than fitting them:

- a rule with zero free parameters that reproduces every measured point is a **stronger claim** than a
  regression that fits them, and needs no held-out validation to be believed;
- it **extrapolates**, because it was derived rather than interpolated;
- it **fails loudly** — which is how the LUTRAM corner became a documented regime, and then a
  predicted one, instead of noise.

And before writing any model at all, ask how many configurations the exploration will actually visit.
If `vecmult` is only ever built at two widths, two syntheses answer every question exactly and a
[lookup](../../guide/resource_model/lookup.md) is the whole model. On `fir_block` that was the
right answer for three of its four modules. **Fitting is the exception, not the default.**

## See also

- [The sweep](./sweep.md) — where these 16 points came from.
- [Resource models](../../guide/resource_model/) — the concepts, of which this page is the instance.
- [Block FIR resource models](../firblock/resource_model.md) — the same method on a composite design
  with different structures.
