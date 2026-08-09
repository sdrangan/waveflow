---
title: Resource models
parent: Block FIR with state
nav_order: 9
has_children: false
audience: python
api: [resource_structure, get_rm, VitisResourceModel, DesignStructure, MultGroup, LutFfBasis, InterfaceResourceModel, add_rm, compose]
summary: "What this design asserts about its own area, declared per module. FirCompute states its structure -- how many multipliers, of what width, and the terms LUT and FF may grow in -- and a stock VitisResourceModel prices it: DSP exactly from device rules, LUT and FF by regression. FirBlock adds only the interface term, a lookup on boundary structure. Three of the five modules declare nothing and keep the inherited lookup, which is the ratio to expect: authoring effort concentrates where area actually moves."
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

Neither input is a measurement, which is why this costs zero fitted parameters and holds outside any
grid. The design says so by declaring the multipliers and letting `dsp_count` price them:

```python
mults = [MultGroup(count=n_mult, operand_bits=samp_w)]
```

It is [exact at all 24 measured points](./resource_fit.md#dsp-the-prior-holds-exactly).

{: .note }
> **The one thing geometry does not explain, and how it is declared.** The packed serial case measures
> a constant `+1` — at every `NTAP` in {8, 16, 32}, so it reads as one multiply that failed to find a
> partner rather than a wrong law. That sentence is the declaration:
>
> ```python
> if not unroll and dsp_per_mult(samp_w, PART) < 1.0:
>     mults.append(MultGroup(count=1, operand_bits=samp_w))
> ```
>
> A group of one, priced by the same rule as the others — `ceil(1 × 0.5) = 1`. It used to be a named
> constant added to a formula. Declaring it as structure is better than either hiding it in the
> arithmetic or leaving it as a fudge: the residual is now *said out loud in the design's own terms*,
> and if a future part pairs it successfully the rule changes the answer without anyone editing a
> constant.

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

The terms are declared with the structure, in the same method:

```python
lut_ff_basis = LutFfBasis(bases=[n_mult, store_bits, mac_bits],
                          names=("n_mult", "store_bits", "mac_bits"))
```

and which of them each counter uses is the model's one configuration:

```python
fit_basis = {"ff":  ["store_bits", "n_mult"],
             "lut": ["n_mult", "store_bits", "mac_bits"]}
```

**Separate bases per counter, which is the one thing this design needs that
[`VecMult`](../vecmult/vitis_resmod.md) does not.** FF tracks storage with a multiplier term for the
MAC pipeline; LUT needs the accumulator width as well. Declaring a term does not oblige every counter
to use it.

**Features are chosen for meaning, not for fit.** `store_bits` is what partitioned storage physically
costs; it is also what lets a *single* model span both kernels, since its length carries the
realization difference.

That choice has a measured price and is kept anyway — see
[the two choices that went against the fit](./resource_fit.md#lut-and-ff-what-the-fit-achieves).

{: .note }
> **Pooled across realizations, on purpose.** Forking the fit serial-vs-unrolled was tried and made LUT
> *worse*: 12 points against 4 free parameters overfits. So the realization forks the module **key**
> — the two bodies are different hardware and are filed separately — but not the regression, which
> carries the difference through `n_mult` and the lane-extended delay line. A basis that spans both is
> a stronger claim than two that each fit half the data.

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

    def resource_structure(self):
        """What the body contains: multiplier groups, no memories, and the LUT/FF terms."""
        ...

    @classmethod
    def get_rm(cls, platform):
        store = ModuleStore(getattr(platform, "dir", None) or COMMITTED_CALIB)
        return VitisResourceModel(name="fir_compute", part=PART, platform=platform,
                                  cls_name="FirCompute", comp_class=cls, store=store,
                                  fit_basis=dict(FITTED_BASIS)).load_or_fit()
```

`get_rm` is a **classmethod**, so a model cannot close over one instance — everything
configuration-specific reaches it through `resource_structure` on whatever component it is asked
about. No sample list is built: the 26 measurements are already filed as records, and
`load_or_fit` reads them.

Then one call attaches everything:

```python
top = elaborate(FirBlock, params)
top.add_rm(platform)            # recurses children-first; every module ends up with a model
est = compose(top)              # needs nothing but the graph
```

Only **two** of the five modules define `get_rm` at all. `FirCmdRx`, `MemRStream` and `MemWStream`
keep the inherited default — a lookup against the platform store — because each was measured once and
its area is a fact to recall rather than a function to fit. Expect that ratio: the authoring effort
concentrates in the few modules whose area actually moves.

| module | model | why |
|---|---|---|
| `MemRStream`, `MemWStream` | lookup (inherited) | one configuration across the whole grid |
| `FirCmdRx` | lookup (inherited) | four, one per sample width |
| `FirCompute` | `VitisResourceModel` | the only module the swept knobs reach |
| `FirBlock` | `InterfaceResourceModel` | its own cost only; children are summed by `compose` |

{: .note }
> **Why on the class, and not installed from outside.** These were briefly attached by assigning onto
> the classes from `fir_block_resource.py`, to keep the design module free of calibration imports. It
> bought nothing — both methods import what they need inside the body, so `fir_block.py` gained no
> module-level dependency either way — and it cost the reader a level of indirection plus a call that
> had to happen before `add_rm` would work. Declared on the class, a design estimates as imported.

What stays in `fir_block_resource.py` is small and is *calibration* rather than design: the part, the
per-counter basis selection, and `dsp_prior` / `bram_prior`, which are no longer used to predict
anything — they are the **oracles the tests check the declaration against**, so the structure and the
law it encodes cannot drift apart.

## See also

- [The sweep and its results](./resource_fit.md) — the measurements these models are checked against.
- [The model kinds](../../guide/resource_model/rm.md) — prior, fitted, lookup and interface in general.
- [The two kernels](./kernels.md) — the bodies the multiplier counts are read off.
- [Fixed point](./fixedpoint.md) — where `acc_bits` comes from.
