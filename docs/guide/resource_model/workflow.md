---
title: Modelling your own design
parent: Resource Models
nav_order: 4
has_children: false
audience: python
api: [InspectSynthStep, ModuleStore, compose, publish_calib]
summary: "End to end for a design of your own: wire the attribution step into your build, sweep a grid, choose a model kind per module (most need a table), encode what physics you have, fit only what is left, validate against held-out totals, and publish so the next design inherits the measurements. Includes the sweep habits worth copying and what each failure mode is telling you."
---

# Modelling your own design

The machinery is general; what varies per design is which modules move and what physics you can encode.
This is the order to do it in.

## 1. Make every synthesis leave a record

Add the attribution rung after your `csynth` step, and have the synthesis step publish its own
wall-clock:

```python
    # in your CSynthStep
    produces = {"report_dir": Path("proj/solution1"), "synth_seconds": None}

dag.add(InspectSynthStep(
    name="resources", comp_class=MyTop, top_name="my_top",
    elaborate_params=("width", "depth", ...),
    params={...}))
```

Do this **before** you think you need a model. The step is nearly free — everything expensive already
happened — and a synthesis whose numbers were not captured is one you will have to run again.

Records land only when the build selects a [platform](../calib/platform.md); without one you still get
`results/resources.json`.

## 2. Sweep a grid

Four habits, each of which exists because the alternative cost someone a run:

- **Vary features independently** — a grid, not a diagonal. Two features that move together are
  collinear and their coefficients cannot be separated.
- **Pre-flight the whole grid without the toolchain.** A `--dry-run` through codegen takes about a
  second and tells you whether every parameter combination even generates.
- **Write incrementally and support resume.** Hours of synthesis should not be lost to one crash.
- **Record failures as failures.** A sweep that quietly covered 19 of 24 points while reporting 24
  leaves a hole in the fitted region — exactly where [confidence](../calib/modules.md) would later
  claim interpolation.

`examples/fir_block/fir_block_sweep.py` is a working template.

{: .warning }
> Sweep into a **work-tier** platform (`calib/work/<name>`) with its own name. Reusing a shipped
> platform's name makes `Platform.resolve` find the *packaged* directory through its fallbacks and
> write there, and only `publish_calib` may do that.

## 3. See which modules actually moved

This is the step people skip, and it decides most of the work:

```python
from collections import Counter
Counter(store.get_identity(k).cls_name for k in store.keys())
```

On the reference design, 24 syntheses produced 30 distinct module configurations — and two modules had
exactly **one** each. A module with one configuration needs a table, not a model. Expect the fitting
work to concentrate in one or two modules.

## 4. Choose a kind per module

| the module… | use | free parameters |
|---|---|---|
| does not vary over your knobs | [lookup](./models.md#lookup) | none |
| has a counter set by a *binding decision* (DSP, BRAM) | [prior](./models.md#prior) | none |
| has LUT/FF that move | [fitted](./models.md#fitted), over structural features | yes |
| is the composite itself | [interface](./models.md#interface) | none |

**Encode physics wherever you can reach it.** A prior that reproduces the corpus exactly is a far
stronger claim than a regression, and it extrapolates where a fit does not. Look for: multiplier counts
and widths against the DSP's geometry, array sizes against block-RAM granularity, and anything a pragma
in your own source determines.

**Then fit only the remainder**, and choose features for *meaning*:

```python
FittedResourceModel(counters=("lut", "ff"),
                    basis={"ff": ["store_bits", "n_mult"], "lut": [...]},
                    feature_fn=my_features)
```

A feature like "bits of partitioned storage" extrapolates because it is what the hardware costs. A raw
parameter that happens to correlate does not.

## 5. Validate against something the fit never saw

Fit on per-module figures; hold out the **design totals**. Then lead with
[decision fidelity](./validation.md) — rank correlation, and picking the right extremum — because that
is the claim exploration needs and the one the numbers support.

Set tolerances just above what the measurement produced, so a regression trips them rather than passing
for years inside a loose bound.

## 6. Publish, so the next design inherits it

```bash
python -m waveflow.calib.publish calib/work/<name> waveflow/calib/platforms/<platform>
#   ...review the plan, then re-run with --apply
```

Dry-run by default; only changed files are written; a coverage-regression guard refuses to replace a
library with one built from fewer measurements. Publish into the platform matching your **part and
clock** — that identity is what makes the records valid, and `Platform.resolve` confirms it.

What this buys is concrete: the shipped `zynq7020_bfm_100mhz` library now carries ~26 minutes of
synthesis as 156 KB of records, and a design composing those modules at those configurations gets
`EXACT` area with no toolchain at all.

## What the failure modes are telling you

| symptom | meaning |
|---|---|
| `UnmappedModuleError` | a module has no row in the report — usually the elaboration parameters do not match what was synthesized. Never skip it: dropping a module inflates the interface term |
| interface term is **negative** | HLS shared logic across a module boundary; additivity is leaking. This is the cross-block surprise whole-design runs exist to catch |
| a lookup reports `UNCALIBRATED` | not a bug — that configuration was never measured, and the model is refusing to guess. It tells you which synthesis to spend next |
| prior misses by a *constant* | one unmodelled instance, not a wrong law. Keep it as a named correction rather than folding it into the formula |
| prior misses by a *factor* | the law is wrong. Re-derive from the device, do not patch |
| whole-design error ≪ per-module error | dilution by exact terms, not model quality. Report both |

## See also

- [Validating a model](./validation.md) — the traps in more detail.
- [Resource analysis](../resource/) — the measurement side this consumes.
- [The calibration workflow](../calib/workflow.md) — the same work → publish flow for *timing*.
