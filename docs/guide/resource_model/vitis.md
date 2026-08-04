---
title: VitisResourceModel
parent: Resource Models
nav_order: 5
audience: python
api: [VitisResourceModel, DesignStructure, MultGroup, MemArray, PerLane, Crossbar, Counter, ReductionTree, dsp_count, bram_estimate, device_for, require_same_device]
summary: "The default model kind for an AMD/Xilinx target. A design declares what it contains as a DesignStructure; the rows backed by a hard primitive are priced exactly by device rules, and the rows that become fabric supply the basis terms for the LUT/FF regression. One declaration, four counters, and the device geometry lives in exactly one place."
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
        multipliers = [MultGroup(count=lw, operand_bits=16)],                    # -> dsp
        memories    = [MemArray(banks=lw, depth=self.vlen // lw, elem_bits=16)], # -> bram
        per_lane    = [PerLane(lanes=lw)],                                       # -> basis term
        crossbars   = [Crossbar(lanes=lw)],                                      # -> basis terms
    )
```

Six fields, in two groups.

### Rows backed by a hard primitive — priced exactly

| declare | means | priced by |
|---|---|---|
| `MultGroup(count, operand_bits)` | *count* signed multiplies of that operand width | `dsp_count` |
| `MemArray(banks, depth, elem_bits, uram=False)` | an array as **you partitioned it**: `banks` is the `ARRAY_PARTITION` factor, `depth` is entries *per bank* | `bram_estimate` |

A group rather than a single multiplier because a datapath replicates one shape — and because a
design with two different widths must be able to say so rather than averaging them. An unpartitioned
array is `banks=1` with the full depth.

### Rows that become fabric — they supply basis terms

| declare | means | contributes |
|---|---|---|
| `PerLane(lanes)` | a datapath replicated per lane: adders, comparators, enables | `n_lane` |
| `Crossbar(lanes)` | **any lane can reach any position** — variable-position mux, barrel shifter | `xbar_sw`, `xbar_depth` |
| `Counter(over)` | a counter or address register naming one of *over* items | `addr_bits` |
| `ReductionTree(lanes)` | a sum/max/and across lanes | `reduce_ops` |

`DesignStructure.basis_terms()` reduces these to fixed names, accumulated across every declared
instance — so two crossbars of different widths sum rather than requiring you to invent a term.

{: .note }
> **There is no second place to state the basis.** Declaring a `Crossbar` is what puts `LW²` in the
> fit. That is what makes the discipline *"a bad held-out error means a missing structure"*
> actionable: you fix the declaration, not the polynomial. See [Fitting](./fit.md).

Note what is **not** in a declaration: no port widths, no block shapes, no thresholds. Only structures
whoever wrote the body already knows, because they wrote them. The base declares a stub that raises
with a pointed message, so a module that never overrides it fails loudly rather than deriving nothing.

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

Equally, a **derived** quantity usually beats a raw parameter. `VecMult` declares `lanes=lw` rather
than `dwid`, because the lane count is what replicates — and it stays right if the sample width ever
changes.

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
| `lut`, `ff` | regression on the declared fabric terms | yes |

Anything outside that — an ASIC platform's `cell_area`, say — is reported by name in the confidence
and downgrades the whole prediction, rather than defaulting to zero.

## Next

- [Binding a model to a design](./getrm.md) — where `resource_structure` and `get_rm` live.
- [Fitting](./fit.md) — how the fabric half gets its coefficients.

## See also

- [FPGA resources](../resource/xilinx.md) — what these primitives are, and the DSP48E1 geometry the
  rules rest on.
- [The VecMult example](../../examples/vecmult/resource_model.md) — this page's contents applied to a
  real design, with measured numbers.
