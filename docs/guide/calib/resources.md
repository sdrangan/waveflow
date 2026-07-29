---
title: Resource measurements
parent: Model calibration
nav_order: 11
has_children: false
audience: python
api: [InspectSynthStep, store_report, SynthReport]
summary: "Filing resource measurements as calibration data: the InspectSynthStep build rung that attributes a csynth report and stores the per-module records, how synthesis cost is recorded rather than modelled, and how to sweep a parameter grid so the library accumulates. The analysis mechanics — reading the report and decomposing a composite — are in Resource analysis; this page is about keeping the results."
---

# Resource measurements

Timing is *fit* — a model form whose coefficients are recovered from a sweep. Resources are
**measured**: the synthesis report says 32 DSPs and that is what it is. What calibration adds is
**keeping** the measurement, keyed so a later design can reuse it instead of paying for another
synthesis.

{: .note }
> This page covers storing and sweeping. For how a report is read and how a composite's total is
> decomposed into modules and interface logic, see [Resource analysis](../resource/) — in particular
> [Composite kernels](../resource/composite.md), which explains the attribution this page files.

## The build step

`InspectSynthStep` is the DAG rung after `csynth`. It attributes the report
([`report_from_solution`](../resource/composite.md)), writes it as JSON, and files the per-module
records into the platform's [module store](./modules.md):

```python
from waveflow.build.resource_steps import InspectSynthStep

dag.add(CSynthStep(name="csynth"))
dag.add(InspectSynthStep(
    name="resources", comp_class=FirBlock, top_name="fir_block",
    elaborate_params=("mem_dwidth", "ntap", "samp_w", "samp_i", "unroll_lane"),
    params={"mem_dwidth": 32, "ntap": 32, "samp_w": 16, "samp_i": 2, "unroll_lane": False}))
```

`elaborate_params` names which of the step's params are *elaboration* parameters — the rest (a
`live_output` flag, say) are the step's own business and must not reach `elaborate`.

Everything expensive already happened: the synthesis is the cost, and this is reading its output. That
is why it is worth running on **every** csynth rather than only when someone remembers to calibrate.
The corollary is that it must never fail a build that synthesized correctly — a report it cannot
attribute is a real error and raises, but a build with no [platform](../platform/identity.md) selected simply
writes `results/resources.json` and says so.

## Recording what it cost

The producing step publishes its own wall-clock as an artifact:

```python
    produces = {"report_dir": Path("fir_block_proj/solution1"), "synth_seconds": None}
```

`InspectSynthStep` consumes it and stamps it into each record's `cost_seconds`, split evenly across the
modules — the synthesis was one indivisible run, and pretending to know each module's share of it would
be inventing data.

Cost is **recorded, never modelled**. A history of real runs answers *"what would recalibrating here
cost?"* better than any estimate could, and it is unrecoverable if not captured at the moment it was
spent.

{: .note }
> `cost_seconds` is machine-local while the measurement is not. A record published from one machine
> carries a duration that means nothing on another, so treat it as provenance ("this took four minutes
> to produce") rather than as a portable prediction.

## Sweeping a grid

`examples/fir_block/fir_block_sweep.py` drives one csynth per design point:

```bash
python -m examples.fir_block.fir_block_sweep --dry-run   # elaborate + codegen only, no Vitis
python -m examples.fir_block.fir_block_sweep             # the full sweep
python -m examples.fir_block.fir_block_sweep --resume    # continue an interrupted one
```

Three habits worth copying into any sweep of your own:

- **Pre-flight the whole grid without the toolchain first.** `--dry-run` runs every point through
  codegen in about a second. Learning that a parameter combination does not generate is worth one
  second, not two hours.
- **Write incrementally and support resume.** Hours of synthesis should not be lost to one crash near
  the end.
- **Record failures as failures.** A sweep that quietly covered 19 of 24 points while reporting 24
  leaves a hole in the fitted region — exactly where [confidence](./modules.md) would later claim
  interpolation.

{: .warning }
> A sweep writes the **work tier** (`calib/work/<name>`), never the tracked library. Give it its own
> platform name: reusing a shipped platform's name makes `Platform.resolve` find the *packaged*
> directory through its fallbacks and write there, and only `publish_calib` may do that. See
> [the calibration workflow](../platform/workflow.md).

## What a sweep buys

The reference sweep over `ntap ∈ {8,16,32} × samp_w ∈ {8,12,16,24} × {serial, unroll}` — 24 points,
about 20 minutes — produced 96 module measurements over only **30 distinct configurations**:

| module | distinct keys across the grid |
|---|---|
| `FirCompute` | 24 — moves with every knob |
| `FirCmdRx` | 4 — sees only `samp_w` |
| `MemRStream` | **1** — sees neither `ntap` nor `samp_w` |
| `MemWStream` | **1** |

The two memory modules were characterized **once** and served all 24 design points. That is the
[structural keying](./modules.md) paying off in syntheses rather than in argument, and it is the
mechanism by which a large design space costs far fewer runs than it has points.

## See also

- [Module keys and the record store](./modules.md) — how a measurement is addressed, verified, filed.
- [Resource analysis](../resource/) — reading the report and decomposing a composite.
- [Platforms](../platform/identity.md) — resource counts are part- and clock-specific, and keyed accordingly.
