---
title: Resource models
parent: Block FIR with state
nav_order: 9
has_children: false
audience: python
api: [resource_structure, get_rm, VitisResourceModel, DesignStructure, MultGroup, LutFfBasis, InterfaceResourceModel, add_rm, compose]
summary: "What this design declares about its own area, and how five modules compose into one estimate. FirCompute states its structure — how many multipliers of what width, that nothing goes in block RAM, and the terms LUT and FF may grow in — and a stock VitisResourceModel prices it. FirBlock declares only the interface term. Three modules declare nothing at all and keep the inherited lookup, which is the ratio to expect: the authoring effort concentrates where area actually moves."
---

# Resource models

This page is what the design *asserts* about its own area, and how those assertions add up across a
hierarchy. What the measurements say about whether they hold is
[The sweep and its results](./resource_fit.md) — kept separate deliberately, so a claim and its
evidence do not get read as one thing.

The general machinery is [Resource Models](../../guide/resource_model/); this is one design's use of
it, and the thing it adds over [`VecMult`](../vecmult/vitis_resmod.md) is **composition**.

## The five modules

`compose` walks the whole hierarchy and sums every module's prediction, so **every module has a
model**. What differs is how much of it anyone had to write:

| module | model | parameters its cost depends on | authored? |
|---|---|---|---|
| `MemRStream`, `MemWStream` | `LookupResourceModel` | `mem_dwidth`, `mem_awidth`, `max_xfer_len`, `inband` | no — the default |
| `FirCmdRx` | `LookupResourceModel` | `mem_dwidth`, `samp_w` | no — the default |
| `FirCompute` | `VitisResourceModel` | `mem_dwidth`, `ntap`, `samp_w`, `unroll_lane` | **yes** |
| `FirBlock` | `InterfaceResourceModel` | `mem_dwidth` only, through the port widths | **yes** |

Only two of the five needed anything written, and the third column is why. **A module's cost depends
only on the parameters that reach it**, not on every knob the design exposes — so a module the
interesting knobs miss is one you can cover by measuring it, rather than by modelling it. A
[lookup](../../guide/resource_model/lookup.md) is exact wherever it answers and `UNCALIBRATED` where
it does not, and neither of those is a guess.

Which of those parameters an exploration actually *varies* decides how much measuring that costs — but
that is a property of [the sweep](./resource_fit.md), chosen later. The models above are complete
before any of it is decided.

{: .note }
> **"The default" is a behaviour, not a shared model.** No module inherits a resource model from
> anywhere. `HwModule.add_rm_self` resolves in one step: use whatever `get_rm(platform)` returns, and
> if the class does not define one, install a `LookupResourceModel` against the platform's record
> store. `FirCmdRx` is a module of this design like any other — it simply has nothing that a
> structural model would buy, so it takes the default.
>
> That still needs **measurements**: a lookup's parameters *are* its table. What it does not need is
> an author.

The rest of this page takes the four rows in turn.

## `FirCompute` — the one that is fitted

`FirCompute` is the module every interesting knob reaches: `ntap`, `samp_w` and `unroll_lane` all
change what it costs, and they are exactly the knobs an exploration wants to turn. Covering that by
measurement means one synthesis per combination, which is the situation a structural model exists for.
So it says what it *contains* and lets a stock
[`VitisResourceModel`](../../guide/resource_model/vitis.md) price it:

```python
def resource_structure(self):
    lw = lane_width(self.mem_dwidth, self.samp_w)
    n_mult = n_multipliers(self.ntap, self.samp_w, self.mem_dwidth, self.unroll_lane)

    mults = [MultGroup(count=n_mult, operand_bits=self.samp_w)]
    if not self.unroll_lane and dsp_per_mult(self.samp_w, PART) < 1.0:
        mults.append(MultGroup(count=1, operand_bits=self.samp_w))   # the one that failed to pair

    acc_bits = 2 * self.samp_w + math.ceil(math.log2(max(2, self.ntap)))
    delay_entries = self.ntap + (lw - 1 if self.unroll_lane else 0)
    return DesignStructure(
        multipliers  = mults,                                        # -> dsp
        # no `memories`: the arrays are partitioned into registers   # -> bram = 0
        lut_ff_basis = LutFfBasis(
            bases=[n_mult, self.samp_w * (self.ntap + delay_entries), n_mult * acc_bits],
            names=("n_mult", "store_bits", "mac_bits")),             # -> lut, ff
    )
```

Three declarations, and each is a claim about the body in [the two kernels](./kernels.md) rather than
about any model. Taken in turn:

### `MultGroup` → DSP {#dsp-geometry-not-statistics}

Two facts settle the DSP count, and neither is statistical.

**The DSP48E1 is a 25×18 signed multiplier**, so one `samp_w × samp_w` multiply costs:

| `samp_w` | ≤ 8 | ≤ 18 | ≤ 25 |
|---|---|---|---|
| DSPs | **0.5** — two multiplies share one | 1 | **2** — one operand exceeds 18 bits, so the product splits |

That half is the device rule's, not this design's. **The kernel supplies the other half** — how many
multiplies there are: `NTAP` for the serial body, `NTAP × LW` for the unrolled one, where
`LW = mem_dwidth // samp_w` (the unrolled body's own comment: *"LW independent windows → LW*NTAP
multipliers"*).

Neither input is a measurement, which is why this costs **zero fitted parameters** and holds outside
any grid. It is [exact at all 24 measured points](./resource_fit.md#dsp-the-prior-holds-exactly).

{: .note }
> **The one thing geometry does not explain, and how it is declared.** The packed serial case measures
> a constant `+1` — at every `NTAP` in {8, 16, 32}, so it reads as one multiply that failed to find a
> partner rather than a wrong law. That sentence *is* the declaration: a second group, of one,
> priced by the same rule as the others (`ceil(1 × 0.5) = 1`).
>
> It used to be a named constant added to a formula. Declaring it as structure beats both hiding it in
> the arithmetic and leaving it as a fudge: the residual is said out loud in the design's own terms,
> and a future part that pairs it successfully changes the answer without anyone editing a constant.

### No `memories` → BRAM = 0 {#bram-a-prior-that-asserts-zero}

The absence is the declaration. The tap and history arrays carry an `ARRAY_PARTITION` from their
[`add_state`](./state.md) declaration, which maps their storage into LUTs and registers — so the
design declares **no** `MemArray`, and `VitisResourceModel` predicts `bram: 0`.

Not a default, and not "no BRAM was observed": it is a falsifiable claim about this design's own
pragmas, so a future configuration that *does* spill into block RAM shows up as a failure rather than
passing unnoticed. Predicting zero and omitting a counter are different things, and the difference
matters — an omitted counter contributes silently and makes a design read as cheaper than it is.

### `LutFfBasis` → LUT and FF {#lut-and-ff-the-estimated-half}

No closed form reaches these, so they are fitted — over **structural** terms rather than raw
parameters:

| term | what it means |
|---|---|
| `n_mult` | multipliers instantiated |
| `store_bits` | taps + delay line, in bits. Partitioned, so it lands in registers. Realization-dependent: serial keeps `NTAP` entries, unrolled keeps `NTAP + LW - 1` |
| `mac_bits` | `n_mult × acc_bits`, where `acc_bits = 2W + ceil(log2 NTAP)` is the width the [format algebra](./fixedpoint.md) derives — pipeline register area, to first order |

Which of them each counter regresses on is the model's one piece of configuration:

```python
fit_basis = {"ff":  ["store_bits", "n_mult"],
             "lut": ["n_mult", "store_bits", "mac_bits"]}
```

**Separate bases per counter is the one thing this design needs that `VecMult` does not.** FF tracks
storage with a multiplier term for the MAC pipeline; LUT needs the accumulator width as well.
Declaring a term does not oblige every counter to use it.

**Terms are chosen for meaning, not for fit.** `store_bits` is what partitioned storage physically
costs; it is also what lets a *single* model span both kernels, since its length carries the
realization difference. That choice has a measured price and is kept anyway — see
[the two choices that went against the fit](./resource_fit.md#lut-and-ff-what-the-fit-achieves).

{: .note }
> **Pooled across realizations, on purpose.** Forking the fit serial-vs-unrolled was tried and made LUT
> *worse*: 12 points against 4 free parameters overfits. So the realization forks the module **key** —
> the two bodies are different hardware and are filed separately — but not the regression, which
> carries the difference through `n_mult` and the lane-extended delay line. A basis that spans both is
> a stronger claim than two that each fit half the data.

### Adding the model to the class

```python
    @classmethod
    def get_rm(cls, platform):
        store = ModuleStore(getattr(platform, "dir", None) or COMMITTED_CALIB)
        return VitisResourceModel(name="fir_compute", part=PART, platform=platform,
                                  cls_name="FirCompute", comp_class=cls, store=store,
                                  fit_basis=dict(FITTED_BASIS)).load_or_fit()
```

A **classmethod**, so a model cannot close over one instance — everything configuration-specific
reaches it through `resource_structure` on whatever component it is asked about. No sample list is
built: the measurements are already filed as records, and `load_or_fit` reads them.

#### What you already have, before any sweep {#before-any-sweep}

Both of those are authored with the module and neither needed a measurement, so the model already
answers — with **zero** syntheses run:

```python
>>> rm.predict(comp)          # ntap=32, samp_w=16, serial
{'dsp': 32, 'bram': 0, 'uram': 0, 'srl': 0}
>>> rm.confidence(comp).level
<ConfidenceLevel.UNCALIBRATED: ...>
```

Both figures are the ones synthesis will later report. LUT and FF are **named as missing rather than
defaulted to zero**, because they have no coefficients yet — which is what the sweep is for.

## `FirCmdRx` — a lookup, and what it looks up

`FirCmdRx` declares no model at all, and it is worth being precise about why that is adequate rather
than lazy.

It has exactly **two** parameters, and that is the whole argument:

```python
>>> identify_instance(cmd_rx).params
{'mem_dwidth': 32, 'samp_w': 16}
>>> identify_instance(cmd_rx).key
'fir_cmd_rx-93cb7e21'
```

`ntap` and `unroll_lane` do not reach it — it frames a command, and neither the tap count nor the
kernel realization changes that job. So however widely an exploration sweeps those two, `FirCmdRx`
elaborates to the same handful of modules, and each one can simply be measured.

That **key**, not the parameter tuple, is what the lookup stores under. The distinction matters: the
key is a digest of the module's **elaborated structure**, so a bound FIFO depth or a differently-wired
port reaches it even though neither is a `HwParam`. Two instances with identical parameters and
different wiring would collide under a parameter key and stay distinct under this one — and a
colliding second measurement would silently overwrite the first.

A configuration the store has not seen returns zeros and `UNCALIBRATED`. A lookup will not
interpolate, which is the right refusal here: nothing guarantees a command receiver's area is smooth
in `samp_w`.

{: .note }
> **A lookup is a model, and it is fitted.** Its parameters are the table: `n_free_params` counts one
> per stored cell, which is exactly why it makes no claim to generalize. What it costs no *authoring*
> is a separate matter from what it costs in measurements.
>
> The two mem-streams are the same story further along: none of `ntap`, `samp_w` or `unroll_lane`
> reaches them, so they are invariant across everything this design explores.

## `FirBlock` — only what is left over

A composite's own cost — `m_axi` adapters, channel FIFOs, control block, DATAFLOW shell — is
everything the design needs beyond its modules. It is an
[`InterfaceResourceModel`](../../guide/resource_model/interface.md): a lookup keyed on **boundary
structure** rather than on parameters.

```python
    @classmethod
    def get_rm(cls, platform):
        table = {}
        for dw, counters in INTERFACE_BY_MEM_DWIDTH.items():
            probe = elaborate(cls, {"mem_dwidth": dw, ...}, name="probe")
            table[boundary_signature(probe)] = dict(counters)
        return InterfaceResourceModel(name="fir_block_interface", table=table, platform=platform)
```

Keyed that way not as a preference but because it is what the measurements say: the term was invariant
across all 24 compute configurations and moved only when the memory word width did. The table is built
by elaborating one probe per measured width and asking each for its signature, so the key is
*computed* the same way the lookup will compute it rather than transcribed.

The choice of key is doing real work here. As a *module*, `FirBlock` depends on all five parameters —
it contains everything that does. But its **own** cost is the ports and channels at its edge, and only
`mem_dwidth` changes those:

```python
>>> boundary_signature(top)[0]        # mem_dwidth = 32
(('MMIFReadMaster', 32), ('MMIFWriteMaster', 32), ('StreamIFMaster', 32), ('StreamIFSlave', 32))
```

Keying on the module key would make this term look like a function of five parameters when it is a
function of one. Keying on the boundary says what it actually depends on — and it is a claim the
measurements support rather than a convenience.

On this design the term is **1984 LUT**, which is **16–30 % of the design total** depending on the
configuration — largest, naturally, where the compute is smallest. It is the second-largest
contributor after the compute at every one of the 24 points. Leaving it out is not a rounding error.

## Composing them

```python
top = elaborate(FirBlock, params)
top.add_rm(platform)      # recurses children-first; every module ends up with a model
est = compose(top)        # a module's own cost, plus the sum of its children, recursively
```

Nothing is passed down and nothing is registered centrally: `add_rm` walks the elaborated graph, and
each model reads its own features off the component it is handed. The composed estimate reports the
**weakest** confidence that fed it — three exact lookups and an exact interface term do not upgrade
the one regression.

{: .note }
> **Why on the class, and not installed from outside.** These were briefly attached by assigning onto
> the classes from `fir_block_resource.py`, to keep the design module free of calibration imports. It
> bought nothing — both methods import what they need inside the body, so `fir_block.py` gained no
> module-level dependency either way — and it cost the reader a level of indirection plus a call that
> had to happen before `add_rm` would work. Declared on the class, a design estimates as imported.

What stays in `fir_block_resource.py` is small, and is *calibration* rather than design: the part, the
per-counter basis selection, and `dsp_prior` / `bram_prior` — which no longer predict anything. They
are the **oracles the tests check the declaration against**, so the structure and the law it encodes
cannot drift apart.

## See also

- [The sweep and its results](./resource_fit.md) — the measurements these models are checked against.
- [`VitisResourceModel`](../../guide/resource_model/vitis.md) — the declaration vocabulary in full.
- [The VecMult example](../vecmult/vitis_resmod.md) — the same shape on a single module, without the
  composition.
- [The two kernels](./kernels.md) — the bodies the multiplier counts are read off.
- [Fixed point](./fixedpoint.md) — where `acc_bits` comes from.
