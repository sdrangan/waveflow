---
title: Platform identity
parent: Platforms
nav_order: 1
audience: python
api: [Platform, PlatformCalib, BuildConfig]
summary: "What a platform IS, and what identifies one. A platform holds the timing and resource models valid for a single target, and is identified by an FPGA part, a synthesis clock, and the resource counters that technology is measured in. Explains why all three matter — the part fixes primitive latencies, the clock fixes the HLS schedule, the memory system fixes the bus law, and the counters differ by technology — and how Platform.resolve confirms a build against a stored identity so the part a design is synthesized for cannot drift from the part its models were fit for."
---

# Platform identity

A measurement is only valid for the hardware it was taken on. A **platform** is that hardware, named:
it holds every timing and resource model valid for one target, and it carries the identity of that
target so nothing can use the models against a different one.

Abstractly, a platform is two things bound together:

- the **identity** — which hardware these numbers describe;
- the **library** — the [timing residuals and area records](./layout.md) measured for it.

This page is about the first. A platform is identified by an FPGA **part**, a **synthesis clock**, and
the **resource counters** that technology is measured in — plus, in practice, its **memory system**,
which the part number does not capture.

## Why part *and* clock (and memory)

Timing fits are cycle counts, and three things move them:

- **The part.** Different device *families* (7-series → UltraScale+ → Versal) have different primitive
  latencies (DSP, BRAM/URAM cascades), so op latencies — and cycle counts — can shift. Within a family
  a faster/slower speed grade usually does *not* change the schedule, as long as the part still meets
  the target clock.
- **The synthesis clock.** HLS schedules to meet `create_clock -period`. Tighten the period and it
  inserts pipeline stages — so the *same* C at a different target period can synthesize to a *different*
  cycle count on the *same* part. The clock is a first-class determinant, not a footnote.
- **The memory system.** The `m_axi` [bus law](../calib/bus_model.md) depends on the interconnect + memory
  controller (an idealized BFM vs. a real DDR controller), which the part number does not capture at
  all. This is why a platform name usually tags the memory (`..._bfm_...`).

Area is part-specific for a different reason: the counters themselves are properties of the device.
A 7-series LUT is a 6-input table and its DSP48E1 is a 25x18 multiplier, so the *same* C synthesizes to
different counts on a different family — and an ASIC flow does not have LUTs or BRAMs at all. Which is
why the counter vocabulary is part of the identity too (see [`res_types`](#the-counter-vocabulary)).

Because two of these (part, clock) fix the kernel cycles, one (memory) fixes the bus law, and the
technology fixes what is even being counted, a platform is **human-named** with a manifest, not a bare
part-number directory — a raw part string collides across clock targets and cannot express the memory
system.

### The two clocks

There are two clocks, and only one of them changes the numbers:

| Clock | Role | Effect on cycles |
|---|---|---|
| sim `Clock(freq=…)` | cycles ↔ seconds converter at the sim boundary | none — residuals are stored in **cycles**, so a re-deploy at a new sim frequency needs no refit |
| synthesis `create_clock -period` | the target HLS schedules to | **changes the schedule → changes the cycles** |

So the platform key needs the **synthesis** clock. `Platform.synth_period_ns` is exactly the
`create_clock -period` the TCL emits (`1e9 / clk_freq`).

## The identity manifest

Each platform directory opens with `platform.json`:

```json
{ "part": "xc7z020clg484-1", "clk_freq_hz": 100000000.0 }
```

This is the **one** source both synthesis and calibration read, so the synthesised part can never drift
from the calibrated part. (Before this manifest existed, the two disagreed — codegen baked `clg484`
while a CLI default said `clg400`.)

### The counter vocabulary

A platform may also declare which resource counters it is measured in:

```json
{ "part": "tsmc45", "clk_freq_hz": 1e9, "res_types": ["cell_area", "macros", "regs"] }
```

Omitted — as it is for every FPGA platform — it means the Vitis/FPGA set
(`lut ff dsp bram uram srl`), so an existing manifest is unchanged and an ordinary platform's file stays
exactly as it was.

It belongs in the identity because a counter set is exactly as technology-specific as the part: an ASIC
flow counts cell area and macro instances, in a *float* rather than a count. This is the seam a
non-FPGA technology enters through — declare a platform, rather than reworking the model layer — and it
is what lets a model's counter names be **validated** rather than merely conventional: naming a counter
the platform does not measure in raises instead of being silently dropped when counters are summed.

## `Platform.resolve` — create or confirm

```python
from waveflow.calib.platform import Platform

plat = Platform.resolve("calib/platforms", "zynq7020_bfm_100mhz",
                        part="xc7z020clg484-1", clk_freq=100e6)
```

`resolve` is the gate:

- **Absent** (no `platform.json`): create the directory and **seed** the manifest from the build's
  part/clock. The platform now exists, ready to be populated by a sweep + publish.
- **Present**: load the stored manifest and **confirm** the build's part/clock against it. A mismatch
  raises `PlatformMismatchError`; under `allow_mismatch=True` it downgrades to a
  `PlatformMismatchWarning`. The **stored** values — what the fit is valid for — win either way.

The returned `Platform` exposes `part`, `clk_freq`, `synth_period_ns`, `dir`, and
`component_dir(<task-body>)` — where a [component residual](../calib/component_residual.md) is stored, keyed by
the component's task-body id.

## Selecting a platform on a build

A build names its platform through [`BuildConfig`](../build/corecomp.md), which resolves it at
construction into `config.platform_info`:

```python
config = BuildConfig(
    root_dir="…",
    platform="zynq7020_bfm_100mhz",   # the platform name
    part="xc7z020clg484-1",           # this build's target — confirmed against the manifest
    clk_freq=100e6,
    platforms_root="calib/platforms", # the project-local primary + write target; resolution falls
                                      # back to the shipped in-package library and a per-user overlay
    allow_platform_mismatch=False,    # raise (default) vs warn on a part/clock mismatch
)
```

Every step then reads `config.platform_info.dir`; nothing restates the part per-step. The same identity
flows into codegen — `render_tcl` takes its `set_part` / `create_clock` from `config.platform_info`
(via `tcl_target`) — so the RTL a calibration measures is synthesised for exactly the part+clock the
platform's fit is valid for.

## The reference platform

`waveflow/calib/platforms/zynq7020_bfm_100mhz/` is the first tracked platform: part `xc7z020clg484-1`,
100 MHz, idealized XSI BFM memory (hence `bfm` in the name). It ships **as package data inside
`waveflow`**, so a `pip`-installed build resolves it with no checkout (the search order is in
[Directory layout](./layout.md#where-platform-directories-live)).

It is the default a project [seeds from](./create.md) rather than recalibrating — accepting that if its
own part differs, the numbers may not reproduce, which the mismatch guard makes a deliberate and visible
choice rather than a silent one. See [Managing a platform](./workflow.md#the-reference-platform-end-to-end)
for how it was built.

## See also

- [The bus-transfer model](../calib/bus_model.md) — the platform-scoped `m_axi` law that lives beside the
  manifest.
- [Component residuals](../calib/component_residual.md) — the per-`(component, platform)` fits under
  `components/`.
- [The calibration workflow](./workflow.md) — the two-tier `work` → `publish_calib` → `platforms` flow.
- [`BuildConfig`](../build/corecomp.md) — the `platform` / `part` / `clk_freq` selector in full.
