---
title: Platform identity
parent: Platforms
nav_order: 2
audience: python
api: [Platform, PlatformCalib, BuildConfig]
summary: "A calibration platform is a named directory with an identity manifest (platform.json = FPGA part + synthesis clock) plus the fitted models valid for that target. Platform.resolve is the create-or-confirm gate: a new platform is seeded from the build's part/clock, an existing one is confirmed against them (PlatformMismatchError, or a warning under allow_platform_mismatch). BuildConfig.platform selects one; the same identity drives the csynth set_part / create_clock, so the synthesised part cannot drift from the calibrated part."
---

# Platforms

A calibrated timing model is only valid for the hardware it was measured on. **What counts as "the
hardware"** — the thing you fix, calibrate against, and then reuse — is a *platform*: an FPGA **part**,
a **synthesis clock**, and a **memory system**. A `Platform` bundles that identity with the fitted
models valid for it.

## Why part *and* clock (and memory)

The fitted numbers are cycle counts, and three things move them:

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

Because two of these (part, clock) fix the kernel cycles and one (memory) fixes the bus law, a platform
is **human-named** with a manifest, not a bare part-number directory — a raw part string collides
across clock targets and can't express the memory system.

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
`waveflow`**, so a `pip`-installed build resolves it with no checkout — `Platform.resolve` searches the
build's `platforms_root` first, then falls back to the `WAVEFLOW_PLATFORM_PATH` env, a per-user library,
and finally this packaged reference. It is the *default* a project can reuse without recalibrating —
accepting that if its own part differs, the cycle counts may not reproduce (the mismatch guard makes
that a deliberate, visible choice). See
[the workflow](./workflow.md#the-reference-platform-end-to-end) for how it was built.

## See also

- [The bus-transfer model](../calib/bus_model.md) — the platform-scoped `m_axi` law that lives beside the
  manifest.
- [Component residuals](../calib/component_residual.md) — the per-`(component, platform)` fits under
  `components/`.
- [The calibration workflow](./workflow.md) — the two-tier `work` → `publish_calib` → `platforms` flow.
- [`BuildConfig`](../build/corecomp.md) — the `platform` / `part` / `clk_freq` selector in full.
