---
title: VitisResourceModel
parent: Resource Models
nav_order: 5
audience: python
api: [VitisResourceModel, DesignStructure, MultGroup, MemArray, PerLane, Crossbar, Counter, ReductionTree, LutFfBasis, dsp_count, bram_estimate, lutram_luts, device_for, require_same_device]
summary: "The default model kind for an AMD/Xilinx target, field by field. A design declares what it contains as a DesignStructure: multiplier groups and partitioned arrays, which device rules price exactly with no free parameters, plus a LutFfBasis naming the quantities LUT and FF are allowed to grow in. Includes how to choose those terms — when you need LW, when you need LW-squared, and when the log2 refinement earns its keep — and how the fit holds out both the counted part and the regimes a counter has no rule for."
---

# `VitisResourceModel`

The default kind on a Vitis target. It encodes the split the [section index](./index.md) opens with —
hard primitives are countable, fabric is not — so a design states its structure once and both halves
follow.

## What you declare

`resource_structure()` is an ordinary method on the **module**, declared beside `kernel_task()` —
because it is a fact about the *design* rather than about any model of it. The multiplies and the
`ARRAY_PARTITION` factor are true whether or not anyone ever estimates resources, and keeping the
declaration next to the body it describes is what stops the two drifting.

```python
def resource_structure(self):
    lw = self.lw
    return DesignStructure(
        multipliers  = [MultGroup(count=lw, operand_bits=16)],                # -> dsp
        memories     = [MemArray(banks=lw, depth=math.ceil(self.vlen / lw),   # -> bram
                                 elem_bits=16)],                              #    or lut
        lut_ff_basis = LutFfBasis(bases=[lw, lw ** 2]),                       # -> lut, ff
    )
```

Three fields, in two groups: the first two are **counted** by device rules with no free parameters,
the third states the **shape** of what is left over so it can be regressed. Nothing else is required,
and a module that never overrides `resource_structure` fails loudly rather than deriving nothing.

## The counted half

### `MultGroup(count, operand_bits)` → `dsp`

| field | what it is | where you get it |
|---|---|---|
| `count` | how many signed multiplies exist in the hardware | multiply expressions in the body × the unroll factor of the loop containing them |
| `operand_bits` | width of the **operands** | the operand type — *not* the product's width, which the rule derives |

A *group* rather than a single multiplier, because a datapath replicates one shape. A design with two
different widths — a coefficient multiply and an address multiply, say — declares **two groups**
rather than averaging them, since the DSP cost per multiply is a step function of width and an average
lands between steps.

### `MemArray(banks, depth, elem_bits, uram=False)` → `bram` **or** `lut`

| field | what it is | where you get it |
|---|---|---|
| `banks` | the `ARRAY_PARTITION` factor | the pragma, directly. An unpartitioned array is `banks=1` |
| `depth` | entries **per bank** | `ceil(extent / banks)` — every bank is allocated to the deepest |
| `elem_bits` | width of one element | the array's element type |
| `uram` | bind to UltraRAM | `True` only if you wrote a `BIND_STORAGE` pragma saying so |

Those numbers price the array **whichever primitive it lands in** — see
[the device rules](#the-device-rules) below. You do not choose between them and you do not declare the
threshold; a bank too shallow for a block simply starts being priced by `lutram_luts` instead.

Note what is **not** in either declaration: no port widths, no block shapes, no thresholds. Only facts
whoever wrote the body already knows, because they wrote them.

## The fitted half: `LutFfBasis`

LUT and FF have no device rule, and the reason decides the whole design of this class:

> **Hard primitives are allocated, so they are countable. Soft fabric is not.**
> A DSP or a block RAM is *requested* — count what you need, look up what the device charges. LUTs and
> flip-flops are what everything else *decomposes into*, and how much a given structure decomposes
> into depends on how the tool shares, retimes and packs. There is no table.

So you state the **shape** rather than the cost:

```python
lut_ff_basis = LutFfBasis(bases=[lw, lw ** 2], names=("lw", "lw2"))
```

| field | what it is |
|---|---|
| `bases` | the term **values** at this configuration, evaluated by the module that knows its own parameters. Any Python expression |
| `names` | optional labels, so a fitted formula reads `c1·lw2` rather than `c1·b1`. Defaults to `b0`, `b1`, … |

The model fits `c0 + c1·bases[0] + c2·bases[1] + …` per counter and finds the coefficients itself.
**Do not declare a constant term**: every regression carries an intercept already, and a column of
ones would be collinear with it.

### Choosing the terms {#choosing-terms}

This is the only genuinely open decision in the declaration, so it is worth doing deliberately. Each
term stands for a structure with a known growth law:

| term | the structure it stands for | declare it when |
|---|---|---|
| *(intercept)* | control that does not scale — an FSM, a command decode, handshakes | **never** — it is automatic |
| `LW` | anything replicated **once per lane** | almost always, in a vectorized design |
| `log2(N)` | a counter or address register naming one of `N` items | the design indexes a large array or counts to a parameterized bound |
| `LW²` | **any-to-any routing** — a lane's data can reach any position | an index into the lanes is a **runtime** value |
| `LW²·log2(LW)` | the *select depth* of that same routing | you already have `LW²` and it is not enough |

The three lane terms, in order of how often it is wrong to omit them:

**`LW` — the default.** If a loop is unrolled `LW` times there are `LW` copies of whatever is inside
it: adders, comparators, enables, per-lane registers. Nearly every vectorized design needs this term,
and a design needing *only* this term is one whose cost is linear in throughput — the comfortable
case.

**`LW²` — the one people miss.** Ask whether any index into your lanes is decided at runtime. If a
beat can place a variable number of lanes at variable positions, the hardware contains a
variable-position mux, and an `LW`-input `LW`-output routing network has `~LW²` switch points. This is
why a design offering a runtime length costs *quadratically* in lane count while an otherwise
identical fixed-length design costs linearly. Omitting it does not give a slightly-off model; on
`VecMult` it gives a [43 % one](../../examples/vecmult/resmodfit.md#why-the-obvious-features-fail),
because a quadratic cost cannot be approximated by a line across an 8× range of `LW`.

**`LW²·log2(LW)` — the refinement.** Each of those `LW`-way muxes needs `log2(LW)` select bits, so the
network has depth as well as width. This is the second-order correction to `LW²` rather than an
independent structure — **so reach for it only after `LW²` alone proves insufficient.** On `VecMult`,
`[LW, LW²]` already fits LUT to 0.25 % held out but FF to only 10.4 %; adding the third term takes FF
to 1.7 % and LUT to exact.

{: .warning }
> **The two quadratic terms are nearly collinear** over any small range of `LW`, with a consequence
> you must not trip over: their fitted coefficients are **not individually meaningful**. On `VecMult`
> the FF coefficients alternate in sign and the model still predicts well. Read the prediction, not
> the coefficients — and never conclude from a negative coefficient that a structure has negative cost.

### How the fit works {#how-the-fit-works}

Ordinary linear least squares in the declared terms, per counter, with three refinements that matter:

**The counted part is held out.** Where a device rule already accounts for part of a counter,
`derived_offset` subtracts it from the measurement before fitting and adds it back when predicting —
so the regression only ever models the fabric it is actually responsible for. The one case today is
storage that landed in distributed RAM: `lut` has that rule, so a LUTRAM-regime point contributes a
usable measurement instead of a contaminating one.

**Rows are chosen per counter, not per corpus.** `fit_rows(df, counter)` decides which measurements a
given counter learns from. The default drops LUTRAM-regime rows from every fitted counter *except*
`lut`, because those counters have no rule for what moved and the row therefore describes hardware
their basis cannot express. It is a **no-op** for a corpus that does not straddle the boundary.

**Validation is held out, always.** Fitted on all your points, even a wrong basis looks good — a model
with as many terms as measurements interpolates them exactly and predicts nothing. Leave-one-out is
the honest number, and it is what every figure above is quoted in.

{: .note }
> **Terms are not free.** Each is another coefficient to determine, so it needs more measurements to
> pin down, and it must be *excited* by your grid: four terms fitted across four distinct lane counts
> is already marginal. If adding a term improves the in-sample fit but not the held-out error, it is
> memorising your grid rather than describing your hardware — take it back out.

### Which parameters to declare {#which-parameters}

This decides whether the model generalizes, and it has one rule:

> **Declare the quantity the hardware is built from, not the one the caller passes in.**

`VecMult` has two lengths and only one of them is hardware:

| | | declared? |
|---|---|---|
| `vlen` | a `HwParam` — the **compile-time bound** on the buffer | **yes** — it sizes the array |
| `n` | a field in the runtime command | **no** — it costs nothing in area |

A design fed only short vectors still pays for the bound it was built with. Declaring `n` would model
a *workload*; declaring `vlen` models the *circuit*. The distinction is checkable:

```python
def test_runtime_length_does_not_change_the_hardware():
    a = elaborate(VecMult, {"dwid": 64, "vlen": 1024}, name="a")
    b = elaborate(VecMult, {"dwid": 64, "vlen": 1024}, name="b")
    assert structure_signature(a) == structure_signature(b)
```

Equally, a **derived** quantity usually beats a raw parameter. `VecMult` bases its terms on `lw`
rather than `dwid`, because the lane count is what replicates — and it stays right if the sample width
ever changes.

## The device rules

The geometry lives in `waveflow.calib.device_rules`, keyed on the **part** — because that is what it
is a property of. A DSP48E1 is 25×18 and a DSP48E2 is 27×18, and a model written against one is
simply wrong on the other.

### DSP

```python
dsp_count(n_mult, operand_bits, part) -> int
```

Three regimes, from the port geometry alone:

| operand width | DSPs per multiply | why |
|---|---|---|
| `<= 8` | **0.5** | two narrow multiplies pack into one DSP — a packing *win* |
| `<= 18` | **1** | fits the ports directly |
| `<= 25` (E1) / `27` (E2) | **2** | one operand exceeds the narrow port, so the product splits |
| beyond | `ceil(w/18) × ceil(w/25)` | both operands split — a documented extrapolation, unmeasured here |

Rounds up once at the end, so a lone packed multiply still costs a whole DSP.

### BRAM

```python
bram_estimate(n_banks, depth, elem_bits, part) -> BramEstimate(blocks, binding, ...)
```

Three things the rule knows and a design should not have to:

**A block has legal port shapes, not a bit budget.** 16-bit elements use the ×18 shape, so one BRAM18
holds **1024** entries — not `18432/16 = 1152`.

**Each bank rounds up independently.** That ceiling is the whole law:

```text
BRAM18 = banks × ceil(depth / entries_per_block)
```

Drop it and `banks` cancels unconditionally. Two regimes fall out of the one formula — *partition-bound*
below the knee (`BRAM = banks`, the data size irrelevant) and *data-bound* above it (`banks` cancels,
partitioning free). A grid that samples only one of them will validate a law it never tested.

**Below a threshold HLS declines block RAM entirely.** Measured on xc7z020: a bank of **1008 bits goes
to LUTRAM, 1024 goes to block RAM**. So the rule returns `blocks=0` with `binding="lutram"` — a
*predicted regime*, not an unmodelled corner.

{: .warning }
> The rule reports a **binding**, not just a number, and says `uncertain` in the band between the two
> measured points rather than picking a side. A caller that needs a count and a caller that needs a
> confidence want different things, and collapsing them is how an under-determined band becomes a
> confident wrong answer.
>
> One limit it states about itself: with a single element width, the corpus behind it cannot
> distinguish "depth ≥ 64" from "bits ≥ 1024" — they coincide at every measured point. It is written
> in bits because 1024 is the round number; confirming that needs a second element width.

### LUTRAM

```python
lutram_luts(n_banks, depth, elem_bits, part) -> int
```

`bram_estimate` returning `blocks=0` is only half an answer: the storage did not disappear, it moved
into fabric. This prices it, from the **same three declared numbers** and with zero fitted parameters:

```text
LUTs = n_banks × depth × elem_bits / 64
```

A SLICEM LUT6 is a 64×1 RAM, so distributed RAM is as countable as a block. Note there is **no
per-bank ceiling** here, and that asymmetry with `bram_estimate` is measured rather than assumed — a
LUT6 splits into two 32×1 RAMs, so shallow banks share one instead of each claiming their own.
Rounding per bank would cost 256 at a 16-bank, 32-deep, 16-bit array where the measured truth is 128.

The payoff is that a design straddling the threshold does not need points excluded from its fit: the
LUTRAM contribution is subtracted from the measurement before fitting and added back when predicting,
so the regression only ever models the fabric it is responsible for. See
[VecMult's corner](../../examples/vecmult/resmodfit.md#lutram-luts) for the verification.

{: .warning }
> **Flip-flops are deliberately not modelled here.** Measured at the corner, the FF cost is *flat in
> depth* — the same at 8192 bits and at 4096 — so it is per-lane registering rather than storage, and
> the available lane counts did not determine a form. A design straddling the threshold should expect
> LUT to be exact and FF to carry the regime error.

### URAM and SRL

`uram` is a **declared** counter, predicted 0 unless a `MemArray(uram=True)` asks for it — because
URAM binding is a design decision (`BIND_STORAGE`), not a device choice. Predicting zero is different
from omitting the counter, and the difference matters: an omitted counter contributes silently.

`srl` is *subsumed*. It is not a separate primitive — it is a LUT in a `SLICEM` spent as a shift
register — so the LUT figures the model is fitted against already account for it.

## Guarding the part

```python
require_same_device(part, measured_on, what="…")   # raises DeviceMismatchError
```

Checking that a part is merely **known** is not enough, and the failure looks like success: an
UltraScale+ part *has* a rule — a different one — so a weak guard accepts it and then prices the
design with 25×18 geometry using coefficients measured on 7-series fabric. Both halves wrong, both
silent.

`DeviceMismatchError` is deliberately distinct from `UnknownPartError`: *"there is a rule and it is
the wrong one"* is the subtler case, and it invalidates the fitted half as well as the derived one.

## What the model covers

| counter | from | free parameters |
|---|---|---|
| `dsp`, `bram`, `uram` | declared structure + device rules | **0** |
| `srl` | subsumed into `lut` | — |
| `lut` | `lutram_luts` for any array in the LUTRAM regime, **plus** a regression for the rest | yes, for the fabric part |
| `ff` | regression on the declared fabric terms | yes |

Anything outside that — an ASIC platform's `cell_area`, say — is reported by name in the confidence
and downgrades the whole prediction, rather than defaulting to zero.

## Appendix: naming structures instead of terms {#named-structures}

`DesignStructure` also accepts four *named structures*, which infer a basis instead of taking one:

| declare | contributes |
|---|---|
| `PerLane(lanes)` | `n_lane` |
| `Crossbar(lanes)` | `xbar_sw`, `xbar_depth` |
| `Counter(over)` | `addr_bits` |
| `ReductionTree(lanes)` | `reduce_ops` |

Terms accumulate across instances under fixed names, so two crossbars of different widths sum. It is
the same arithmetic `LutFfBasis` expresses directly — `Crossbar(lanes=lw)` *is* `[lw², lw²·log2(lw)]`
— reached by naming the structure and letting a fixed dictionary supply its form.

Declaring both raises. Two sources for one basis is two things to keep in step, and the failure mode
is a silently double-counted term.

**Prefer `LutFfBasis`.** The named form reads well when a design happens to be in the dictionary and
badly when it is not, and it asks an author to learn a taxonomy in order to say something they can say
in arithmetic. It remains supported because the mapping above is genuinely the right *reasoning* —
[choosing the terms](#choosing-terms) is that dictionary, applied by hand.

## Next

- [Binding a model to a design](./getrm.md) — where `resource_structure` and `get_rm` live.
- [Fitting](./fit.md) — how the fabric half gets its coefficients.

## See also

- [FPGA resources](../resource/xilinx.md) — what these primitives are, and the DSP48E1 geometry the
  rules rest on.
- [The VecMult example](../../examples/vecmult/vitis_resmod.md) — this page's contents as a walkthrough:
  what the class does for you, and where each declared number is read off the kernel body. Its
  [next page](../../examples/vecmult/resmodfit.md) has the measured numbers.
