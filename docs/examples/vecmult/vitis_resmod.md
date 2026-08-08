---
title: The resource model
parent: Vector multiply resource modeling
nav_order: 6
audience: python
api: [VitisResourceModel, DesignStructure, MultGroup, MemArray, LutFfBasis, resource_structure, get_rm]
summary: "VecMult's resource model, before anything is measured. Its two knobs map onto a structure declaration — LW multipliers and LW memory banks of vlen/LW, both read straight off vec_mult_task.h — plus a LutFfBasis saying which quantities LUT and FF are allowed to grow in. get_rm installs a VitisResourceModel that prices the first two exactly with zero syntheses run and fits the last two; the basis is chosen by declare-validate-add rather than by reasoning alone."
---

# The `VitisResourceModel`

To model the resources for the VecMult hardware module, we use the [`VitisResourceModel`](../../guide/resource_model/vitis.md), a resource model for an AMD/Xilinx target.  The Vitis resource model builds a model of the FPGA resource primitives --  LUTS, FF, DSP, and BRAM -- in terms of the **design structure**.

To use `VitisResourceModel`, the `HwModule` class -- in this case, `VecMult` -- must define a function  `resource_structure`.  For `VecMult`, the function is of the form:

```python
def resource_structure(self):
    lw = self.lw
    return DesignStructure(
        multipliers = [MultGroup(count=lw, operand_bits=SAMP_W)],                    # -> dsp
        memories    = [MemArray(banks=lw, depth=math.ceil(self.vlen / lw),           # -> bram
                                elem_bits=SAMP_W)],                                  #    or lut
        lut_ff_basis = LutFfBasis(bases=[lw, lw**2, lw**2 * math.log2(lw)]),     # -> lut, ff
    )
```

Each element of the `DesignStructure` class specifies a structural element (e.g., multipliers, memories, etc.) in terms of the hardware module's parameters.  Details of each element are given in the [guide page for the `VitisResourceModel`](../../guide/resource_model/vitis.md).
For the specific case of the `VecMult` class:

### `MultGroup(count=lw, operand_bits=SAMP_W)` → DSP {#multgroup}

The `MultGroup` element describes the number of multipliers in the design.  In the VecMult task, `vec_mult_task.h`, the multipliers appear in the `MULT` loop:

```c
MULT:
    for (int i = 0; i < n; i += LW) {
#pragma HLS PIPELINE II=1
#pragma HLS LOOP_TRIPCOUNT max=VLEN
        const int nlane = (n - i < LW) ? (n - i) : LW;
        vm_au::read_stream_lane<DWID>(s_in, ylane, nlane);
        for (int j = 0; j < LW; ++j) {
#pragma HLS UNROLL
            ap_int<2 * SAMP_W> p = buf[i + j] * ylane[j];
            zlane[j] = (samp_t)p;
        }
        vm_au::write_stream_lane<DWID>(zlane, z_out, nlane);
    }
```

The `MULT` loop multiplies one sample per lane and is unrolled to `LW`, so there are `LW` multipliers.
`operand_bits` is the width of the **operands** — here `buf[i + j]` and `ylane[j]`, both `SAMP_W`
wide. It is *not* the width of the `2 * SAMP_W` product they widen into: how a 16-bit multiply lands
on a DSP48E1 is the device rule's business, not `VecMult`'s.

### `MemArray(banks=lw, depth=ceil(vlen / lw), elem_bits=SAMP_W)` → BRAM **or** LUTRAM {#memarray}

The `MemArray` element describes the **BRAM and LUTRAM storage** — one declaration, because which of
the two an array lands in is not your decision. You identify every array in the kernel that could be
mapped to memory; the device rule decides where each one goes and prices it there. In the `VecMult`
example the only such array is `buf`, holding `x`. Its definition is also in `vec_mult_task.h`:

```c
samp_t buf[VLEN];
#pragma HLS ARRAY_PARTITION variable=buf cyclic factor=LW dim=1
```

From this definition, we can obtain the items for the `MemArray` element:

- `banks` is the partition factor, which is `LW` here, from the `HLS ARRAY_PARTITION ... factor=LW`
- `depth` is the entries *per bank*, which is `ceil(VLEN / LW)`
- `elem_bits` is the width of each element, `SAMP_W` — the width of `samp_t`

Note **`VLEN`, not `n`**: the buffer is sized by the compile-time bound, so that is what the hardware
is priced against — a design fed only short vectors still pays for the bound it was built with.

Those same three numbers price the array **either way it binds**:

| if the bank is | it binds to | and costs |
|---|---|---|
| ≥ 1024 bits (`depth × elem_bits`) | block RAM | `banks × ceil(depth / entries_per_block)` BRAM18, and **0** LUT |
| ≤ 1008 bits | distributed RAM | **0** BRAM, and `banks × depth × elem_bits / 64` LUT |

Both are exact, both have zero fitted parameters, and you declare nothing extra to get the second — a
`MemArray` shallow enough to leave block RAM simply starts being priced by
[`lutram_luts`](./resmodfit.md#lutram-luts) instead of `bram_estimate`. `VecMult` hits this at
`vlen=512, dwid=256`, where banks are 32 deep and the buffer costs **0 BRAM and +128 LUT**.

### `LutFfBasis(bases=[...])` → LUT and FF {#perlane}

DSP and BRAM are counted exactly. LUT and FF cannot be — there is no table saying how many LUTs a
given structure becomes, because that depends on how the tool shares, retimes and packs. So they are
**fitted**, and what you declare is the shape you expect them to grow in:

```python
    lut_ff_basis = LutFfBasis(bases=[lw, lw ** 2], names=("lw", "lw2"))
```

`VitisResourceModel` then fits a linear model in those terms — `c0 + c1·lw + c2·lw²` — and finds the
coefficients itself. You are not choosing coefficients, only which quantities the cost is allowed to
depend on. (No constant term is declared: every regression carries an intercept already.)

For `VecMult`, two terms suggest themselves from the kernel:

- a term **linear in `LW`**, because the `MULT` loop is unrolled `LW` times, so whatever one lane
  costs there are `LW` copies of it;
- less obviously, a term in **`LW²`**. `n` is a *runtime* length, so the ragged final beat has to
  place a variable number of lanes at variable positions — and any-lane-to-any-position routing
  grows quadratically, not linearly.

{: .note }
> **The `LW²` term is the one people miss**, and it is worth knowing why it is there rather than
> taking it on faith. Without it the only honest guess is that logic scales with datapath width,
> which fits [43 % off](./resmodfit.md#why-the-obvious-features-fail). The measurements say so before
> any fitting: LUT grows 964 → 1370 → 2622 → 6956 across the four widths, where linear would predict
> roughly 964 → 1930 → 3860.

### When you are not sure, add a term and check {#add-a-term-and-check}

You do not have to get the basis right by reasoning alone — that is what held-out validation is for.
Fit it, look at the error on points the fit did not see, and add a term if it is not good enough.
`VecMult` is a live example, because the two-term basis above is **not** the one it ships:

| basis | LUT (worst held out) | FF (worst held out) |
|---|---|---|
| `[lw, lw²]` | **0.25 %** | 10.44 % |
| `[lw, lw², lw²·log2(lw)]` | **0.00 %** | **1.73 %** |

Two terms are already enough for LUT. FF is off by 10 %, which says its growth has a shape those two
terms cannot make — so a third goes in, and FF lands at 1.7 %. That is the whole loop: *declare,
validate, add, re-validate.*

```python
    lut_ff_basis = LutFfBasis(bases=[lw, lw ** 2, lw ** 2 * math.log2(lw)],
                              names=("lw", "lw2", "lw2_log_lw"))
```

{: .warning }
> **Terms are not free.** Each one is another coefficient to determine, so it needs more measurements
> to pin down — and a basis with as many terms as you have design points will fit them perfectly and
> predict nothing. That is why the error above is quoted **held out**: fitted on all 16 points, even a
> bad basis looks good. If adding a term improves the in-sample fit but not the held-out error, the
> term is memorising your grid rather than describing your hardware.

### The two that stay empty

`counters` and `reductions` are not declared: `VecMult` reduces nothing (its output is element-wise)
and has no address register worth pricing separately.

{: .note }
> `MultGroup` and `MemArray` are priced **exactly**, by device rules with zero fitted parameters —
> DSP, BRAM, and the LUTs an array costs when it lands in distributed RAM instead. `LutFfBasis`
> supplies the terms for the LUT/FF regression, which covers the *rest* of the fabric. That split is a
> property of the silicon rather than a choice — anything allocated is countable, and everything else
> is what the tool decomposes logic into — and the
> [guide](../../guide/resource_model/vitis.md) covers it in full.

## Adding the model to the class

The declaration says what the design contains; `get_rm` says which model prices it. It is a
**classmethod**, so it cannot close over an instance — everything configuration-specific reaches the
model through `resource_structure()` on whatever component it is asked about, which is why one object
prices every `(dwid, vlen)` of the class.

```python
    @classmethod
    def get_rm(cls, platform):
        part = getattr(platform, "part", None) or PART
        require_same_device(part, PART, what="VecMult's resource model")

        store = ModuleStore(getattr(platform, "dir", None) or COMMITTED_CALIB)
        return VitisResourceModel(name="vec_mult", part=part, platform=platform,
                                  cls_name="VecMult", comp_class=cls, store=store,
                                  shell=vec_mult_shell(store)).load_or_fit()
```

The class is the **stock** `VitisResourceModel` — no subclass to write. The `store` is the important
argument: it is the record library the
[sweep files its measurements into](./sweep.md#where-the-measurements-go), and it is where the fit
gets its data. `load_or_fit` takes no sample list because it does not need one; it resolves in three
steps, cheapest first:

1. **A published artifact**, if one exists at the model's `params_path` — coefficients already
   fitted, so nothing is refitted on every elaboration.
2. **Explicit `samples`**, if you pass them. You should not need to.
3. **The corpus**, reduced from the `store` on demand. **This is the normal path.**

`COMMITTED_CALIB` is the example's own record library, used when the caller supplies no platform —
`add_rm(None)`, the path the toolchain-free tests take. Falling back to it rather than to a
hand-written list of measurements is deliberate: **a second copy of the same numbers is a second
thing to keep in step**, and the records are already there.

From there the model prices any configuration you ask for:

```python
top = elaborate(VecMult, {"dwid": 64, "vlen": 4096}, name="vec_mult")
top.add_rm(platform)          # once, on the top — it recurses the whole hierarchy
est = compose(top)
```

`require_same_device` refuses a platform on a **different device**, not merely an unrecognized one.
Accepting any *known* part would price an UltraScale+ design with 7-series geometry, and both halves
of the model would be wrong silently.

{: .note }
> **You do not subclass the model.** `VecMult` used to ship a `VecMultResourceModel`, and everything
> in it turned out to be general: the integration term, which every design has, and a rule keeping
> the LUTRAM-regime row out of the FF fit. Both are `VitisResourceModel` behaviour now — `lut` keeps
> that row because [`lutram_luts`](./resmodfit.md#lutram-luts) prices what moved into fabric, and any
> counter without such a rule drops it. That default is a **no-op** for a design whose corpus does not
> straddle the boundary, which is most of them.

## What you already have, before any sweep {#before-any-sweep}

`resource_structure` and `get_rm` are both authored with the module, and neither needed a
measurement. So the model already answers — with **zero** syntheses run:

```python
>>> rm = VitisResourceModel(name="vec_mult", part=PART, cls_name="VecMult")
>>> rm.predict(top)
{'dsp': 4, 'bram': 4, 'uram': 0, 'srl': 0}
>>> rm.confidence(top).level
<ConfidenceLevel.UNCALIBRATED: ...>
>>> rm.confidence(top).facts["summary"]
'VecMult: vec_mult has not been fitted'
```

Those DSP and BRAM figures are the ones the synthesis will later report. Half the answer came out of
the declaration alone.

And the other half is **named as missing rather than defaulted to zero** — `UNCALIBRATED`, because
LUT and FF have no coefficients yet. That is the honest failure mode: a model that quietly returned
the two counters it could derive would make the design read as *cheaper* than it is, which is the one
direction an area estimate must not err.

So the declaration is what you write first, and the missing half is what the sweep is for.

## Next

- [The sweep](./sweep.md) — 16 design points through the build DAG, giving LUT and FF the
  measurements they need.
- [How well it fits](./resmodfit.md) — the model with both halves, checked against points it was not
  trained on.

## See also

- [`VitisResourceModel`](../../guide/resource_model/vitis.md) — the general page: the full declaration
  vocabulary, and the device rules (`dsp_count`, `bram_estimate`) regime by regime.
- [Binding a model to a design](../../guide/resource_model/getrm.md) — why `resource_structure` and
  `get_rm` are both methods on the module.
