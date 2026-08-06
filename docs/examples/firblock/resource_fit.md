---
title: The sweep and its results
parent: Block FIR with state
nav_order: 10
has_children: false
audience: python
api: [fir_block_sweep, points, compose]
summary: "What 24 syntheses in 20 minutes actually showed. The DSP prior lands exactly at every point with zero fitted parameters; the LUT/FF fit is 9.8%/7.1% mean under leave-one-out; the composed whole-design estimate is validated against totals that trained none of it, with the warning that the 3.2% design-level figure flatters the model because most of the design is known rather than predicted. Includes the two results that only appear because the design has coupled step functions in it -- the DSP packing win at 8 bits, and the unrolled plateau that is two effects cancelling -- and a design finding: the right realization inverts with sample width."
---

# The sweep and its results

The models are [Resource models](./resource_model.md). This page is the evidence: what a sweep cost,
what it bought, and where each model held or did not.

This design is the worked example behind [Resource Models](../../guide/resource_model/) precisely
because its knobs are awkward. Sample width moves the DSP cost of a multiply *and* the number of
samples per memory word, in **opposite directions** — so a naive fit and a physical model agree on
everything measured here and disagree off the grid, which is exactly the case worth having a worked
example of.

## The measurement

```bash
python -m examples.fir_block.fir_block_sweep --dry-run   # pre-flight the grid, no Vitis
python -m examples.fir_block.fir_block_sweep             # 24 points
```

`ntap ∈ {8,16,32}` × `samp_w ∈ {8,12,16,24}` × `{serial, unrolled}`, at `mem_dwidth=32`:
**24/24 in 20.5 minutes**, ~51 s per point. Every run attributes its report
([Composite kernels](../../guide/resource/composite.md)) and files per-module records.

The numbers are committed twice over, so nothing here needs re-running:
`examples/fir_block/fir_block_corpus.py` holds the grid as source, and the records themselves are
published into the example's platform library.

### What the sweep bought

24 syntheses produced 96 module measurements over only **30 distinct configurations**:

| module | distinct keys | |
|---|---|---|
| `FirCompute` | 24 | moves with every knob |
| `FirCmdRx` | 4 | sees only `samp_w` |
| `MemRStream` | **1** | sees neither `ntap` nor `samp_w` |
| `MemWStream` | **1** | |

The two memory modules were characterized **once** and served all 24 points — the
[structural keying](../../guide/calib/modules.md) paying off in syntheses rather than in argument.

## DSP: the prior holds exactly

[The geometry](./resource_model.md#dsp-geometry-not-statistics) predicts **all 24 points exactly, with
zero fitted parameters.**

| serial | w=8 | w=12 | w=16 | w=24 | | unrolled | w=8 | w=12 | w=16 | w=24 |
|---|---|---|---|---|---|---|---|---|---|---|
| ntap=8 | 5 | 8 | 8 | 16 | | ntap=8 | 16 | 16 | 16 | 16 |
| ntap=16 | 9 | 16 | 16 | 32 | | ntap=16 | 32 | 32 | 32 | 32 |
| ntap=32 | 17 | 32 | 32 | 64 | | ntap=32 | 64 | 64 | 64 | 64 |

### Two things in that table worth pausing on

**At 8 bits the serial kernel uses *fewer* DSPs than it has taps** — `NTAP/2 + 1`. HLS packs two 8-bit
multiplies into a single DSP48. That is a step function going the *helpful* direction, and the opposite
of the packing cliff one expects at the wide end.

**The unrolled column is flat at `2·NTAP` regardless of width** — which looks like a plateau worth
hard-coding, and is not. Lane count falls as width rises while DSP-per-multiply rises, and over this
device's boundaries they cancel exactly:

```text
    samp_w   LW = 32//w   DSP/mult   product
       8         4          0.5      2·NTAP
      12         2          1        2·NTAP
      16         2          1        2·NTAP
      24         1          2        2·NTAP
```

Hard-coding `2·NTAP` would be indistinguishable on this grid and **wrong at `mem_dwidth=64`**, where
the product is 4. This is the clearest argument in the repo for encoding physics rather than fitting a
curve: the fit and the model agree on all the data you have and disagree on the data you do not.

## BRAM: the assertion holds

No module reported any BRAM at any point — which is what
[the prior asserts](./resource_model.md#bram-a-prior-that-asserts-zero), rather than merely what was
observed. The design's two BRAMs are in the interface, not the modules.

## LUT and FF: what the fit achieves

Leave-one-out over the grid:

| | mean | worst |
|---|---|---|
| FF | 7.1% | 18.8% |
| LUT | 9.8% | 24.8% |

FF does well because `store_bits` alone correlates **0.985** with it — partitioned arrays really are
registers. LUT is the honest limit of the approach.

{: .note }
> **Two choices that went against the fit.** Forking the model by realization was tried and made LUT
> *worse* (30.7% vs 24.6%) — 12 points against 4 free parameters overfits — so the realization forks the
> module *key* but not the regression, which carries the difference in its features. And the
> physically-correct storage feature fits FF marginally worse than a sloppier one; it is kept, because a
> feature chosen for meaning extrapolates and one chosen for fit does not.

## Composing, and validating

```python
top = elaborate(FirBlock, params)
top.add_rm(platform)
est = compose(top)

est.total     # {'lut': 9424, 'ff': 11398, 'dsp': 32, 'bram': 2}   predicted
              # measured:    8674 / 11347 /  32 /  2
```

Validated against **design totals that fit nothing** — only per-module figures train the models:

| counter | error | rank correlation vs synthesis |
|---|---|---|
| DSP | **24/24 exact** | 1.000 |
| BRAM | **24/24 exact** | — |
| LUT | 3.2% mean, 8.6% worst | 0.950 |
| FF | 2.8% mean, 8.7% worst | 0.990 |

{: .note }
> Rank correlation here is Pearson over `argsort(argsort(·))` ranks. The convention matters at the
> third decimal: the predictions contain ties, and tie-corrected Spearman gives 0.947 / 0.989 on the
> same data. Every figure in this table is recomputed from the committed corpus by
> `tests/docs/test_documented_numbers.py`, so a model change that moves one fails a test naming this
> page.

...and the DSP-minimal design is identified correctly.

{: .warning }
> **The 3.2% is not the model's accuracy.** The compute module's own held-out LUT error is 9.8% mean /
> 24.8% worst; the whole-design figure is better because the interface term and the three static
> modules are *exact* and dilute the single fitted module. Most of this design is known rather than
> predicted. Quote the per-module error when describing the model, and the design-level error when
> describing what a composed estimate delivers — see [Validating a model](../../guide/resource_model/fit.md#validating).

## A design finding, for free

At `samp_w=8` the serial kernel uses **5 DSPs against the unrolled kernel's 16** — 3.2× — while at
`samp_w=24` both use `2·NTAP` and unrolling costs no DSPs at all.

**The right realization inverts with sample width.** At narrow widths the serial kernel is dramatically
cheaper in DSPs (and slower); at wide widths unrolling is free in DSPs and strictly faster. That is not
a conclusion anyone would reach from reading either kernel, and it fell out of the first sweep — which
is the argument for having the model at all.

## See also

- [Resource models](./resource_model.md) — the four models these measurements check.
- [Composite kernels](../../guide/resource/composite.md) — how a report becomes per-module numbers.
- [Validating a model](../../guide/resource_model/fit.md#validating) — the general form of the check above.
- [The two kernels](./kernels.md) — the serial and unrolled bodies the DSP counts come from.
