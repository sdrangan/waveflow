---
title: Resource modelling
parent: Block FIR (state + fixed point)
nav_order: 9
has_children: false
audience: python
api: [dsp_prior, compute_features, fir_compute_fitted, compose]
summary: "The worked resource-model narrative on this design: a 24-point sweep in 20 minutes, an analytical DSP prior exact at every point with zero fitted parameters, a LUT/FF fit over structural features, and a composed whole-design estimate validated against totals that fit nothing. Includes the two results that only showed up because the design has coupled step functions in it — the DSP packing win at 8 bits, and the unrolled plateau that turns out to be two effects cancelling."
---

# Resource modelling

This design is the worked example behind [Resource Models](../../guide/resource_model/). It is a good
one for that job precisely because its knobs are awkward: sample width moves the DSP cost of a multiply
*and* the number of samples per memory word, in opposite directions, so a naive fit and a physical
model disagree in ways you can see.

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
published into the shipped platform library.

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

## DSP: a prior, exact, with nothing fitted

DSP is a *binding decision*, so it follows the device rather than statistics. Two facts suffice:

**The DSP48E1 is a 25×18 signed multiplier**, so one `samp_w × samp_w` multiply costs

| `samp_w` | ≤ 8 | ≤ 18 | ≤ 25 |
|---|---|---|---|
| DSPs | **0.5** — two multiplies share one | 1 | **2** — one operand exceeds 18 bits, so the product splits |

**The kernel says how many multiplies it has** — `NTAP` serial; `NTAP × LW` unrolled, where
`LW = mem_dwidth // samp_w` (the unrolled body's own comment: *"LW independent windows → LW*NTAP
multipliers"*).

```python
DSP = ceil(n_mult × dsp_per_mult(samp_w))
```

**Result: exact at all 24 points, zero fitted parameters.**

| serial | w=8 | w=12 | w=16 | w=24 | | unrolled | w=8 | w=12 | w=16 | w=24 |
|---|---|---|---|---|---|---|---|---|---|---|
| ntap=8 | 5 | 8 | 8 | 16 | | ntap=8 | 16 | 16 | 16 | 16 |
| ntap=16 | 9 | 16 | 16 | 32 | | ntap=16 | 32 | 32 | 32 | 32 |
| ntap=32 | 17 | 32 | 32 | 64 | | ntap=32 | 64 | 64 | 64 | 64 |

### Two things in that table worth pausing on

**At 8 bits the serial kernel uses *fewer* DSPs than it has taps** — `NTAP/2 + 1`. HLS packs two 8-bit
multiplies into a single DSP48. That is a step function going the *helpful* direction, and it is the
opposite of the packing cliff one expects at the wide end.

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

{: .note }
> One thing the physics does **not** explain: a constant `+1` in the serial packed case. It is constant
> across every `NTAP`, so it is one multiply that failed to pair rather than a wrong law — kept as a
> named constant (`SERIAL_PACK_CORRECTION`) so it stays visibly unexplained instead of being absorbed
> into the formula.

## BRAM: a prior that asserts zero

No module reported any BRAM at any point. The tap and history arrays carry `ARRAY_PARTITION` from their
[`add_state`](./state.md) declaration, so their storage lands in LUTs and registers. The prior returns
`0` **deliberately**, so a future configuration that *does* spill into block RAM shows up as a prior
failure rather than passing unnoticed. The design's two BRAMs are in the interface, not the modules.

## LUT and FF: the fitted half

These are the genuinely estimated counters — partitioned storage, pipeline registers, the accumulate
tree, address and mux logic. Fitted, but over **structural** features rather than raw parameters:

| feature | what it means |
|---|---|
| `n_mult` | multipliers instantiated |
| `store_bits` | taps + delay line, in bits. Partitioned, so it lands in registers. The delay line is realization-dependent: serial keeps `NTAP` entries, unrolled keeps `NTAP + LW - 1` |
| `acc_bits` | `2W + ceil(log2 NTAP)` — the width the [format algebra](./fixedpoint.md) derives |
| `mac_bits` | `n_mult × acc_bits` — pipeline register area to first order |

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

Five models: a prior + fit for `FirCompute`, lookups for the three static modules, and an interface
model for the composite's own cost.

```python
est = compose(elaborate(FirBlock, params), model_for)
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

...and the DSP-minimal design is identified correctly.

{: .warning }
> **The 3.2% is not the model's accuracy.** The compute module's own held-out LUT error is 9.8% mean /
> 24.8% worst; the whole-design figure is better because the interface term and the three static
> modules are *exact* and dilute the single fitted module. Most of this design is known rather than
> predicted. Quote the per-module error when describing the model, and the design-level error when
> describing what a composed estimate delivers — see [Validating a model](../../guide/resource_model/validation.md).

## A design finding, for free

At `samp_w=8` the serial kernel uses **5 DSPs against the unrolled kernel's 16** — 3.2× — while at
`samp_w=24` both use `2·NTAP` and unrolling costs no DSPs at all.

**The right realization inverts with sample width.** At narrow widths the serial kernel is dramatically
cheaper in DSPs (and slower); at wide widths unrolling is free in DSPs and strictly faster. That is not
a conclusion anyone would reach from reading either kernel, and it fell out of the first sweep — which
is the argument for having the model at all.

## See also

- [Resource Models](../../guide/resource_model/) — the general machinery this instantiates.
- [Composite kernels](../../guide/resource/composite.md) — how a report becomes per-module numbers.
- [The two kernels](./kernels.md) — the serial and unrolled bodies the DSP counts come from.
