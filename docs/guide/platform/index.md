---
title: Platforms
parent: Guide
nav_order: 10.5
has_children: true
audience: python
api: [Platform, PlatformCalib, BuildConfig, waveflow_calib]
summary: "A platform is the target a design is built and measured against: an FPGA part, a synthesis clock, and the resource counters that technology is measured in — plus a directory holding everything fit or measured for it. It is not a calibration-only concept: the same identity drives the csynth set_part / create_clock, so the part a design is synthesized for cannot drift from the part its models were fit for. Covers the identity, the directory layout, and the commands that create, inspect and publish one."
---

# Platforms

A **platform** is the target a design is built and measured against:

```text
identity        an FPGA part, a synthesis clock, and the resource counters it is measured in
library         everything fit or measured for that target — bus law, timing residuals, area records
```

Both halves matter, and it is the pairing that makes the whole thing work: a cycle count or a LUT
count is only meaningful *for a particular part at a particular clock*, so the numbers and the
identity they were taken under are stored together and confirmed on use.

## It is not just a calibration concept

Four things consume a platform, and only one of them is calibration:

| consumer | what it takes |
|---|---|
| **the build** | `part` and the clock period → the csynth `set_part` / `create_clock` |
| [timing models](../timing_model/) | the component residual fit for this target |
| [resource models](../resource_model/) | the measured area records, and the counter vocabulary |
| [calibration](../calib/) | where a new fit is written |

The first is the one that is easy to miss. `BuildConfig(platform=…)` resolves a `Platform`, and codegen
reads `part` off it to emit the synthesis TCL — so **someone who never calibrates anything still needs
a platform** to synthesize against the right device. That is why this is its own section rather than a
chapter of model fitting: it is shared infrastructure, and putting it under either the timing or the
resource axis would make the other a second-class citizen of it.

The payoff is a property worth stating plainly:

{: .note }
> The part a design is **synthesized** for and the part its models were **fit** for come from the same
> object, so they cannot drift. `Platform.resolve` is a create-or-confirm gate: a new platform is
> seeded from the build's part/clock, an existing one is *checked* against them, and a mismatch is an
> error rather than a silently reused number.

## What lives in one

```text
<platform>/
    platform.json                       identity: part, clk_freq_hz, [res_types]
    mm_bus.json  points/                the m_axi bus-transfer law and its corpus
    components/<task-config>/           TIMING residuals, keyed by task configuration
    modules/<module-key>/               RESOURCE records, keyed by module structure
```

Two different keys, because the two axes ask different questions — see
[Directory layout](./layout.md), which also covers the tracked/untracked split and which directories
are yours versus the package's.

## Getting one

```bash
waveflow_calib new  calib/platforms/myboard --part xc7z020clg484-1 --clk 100e6
waveflow_calib show calib/platforms/myboard
```

Waveflow ships one reference platform (`zynq7020_bfm_100mhz`) so an installed user inherits a working
bus law, two component residuals, and 35 measured module configurations with no toolchain run at all.
See [Managing a platform](./workflow.md).

## In this section

- [Directory layout](./layout.md) — what is in a platform, which tier it lives in, what is tracked.
- [Platform identity](./identity.md) — the manifest, the mismatch gate, and why cycle counts are only
  valid for the part they were fit against.
- [Managing a platform](./workflow.md) — the `waveflow_calib` commands, and the two-tier
  work → publish flow that is the only writer of a tracked library.

## See also

- [Model calibration](../calib/) — how the fits stored here are produced.
- [`BuildConfig`](../build/corecomp.md) — the `platform` / `part` / `clk_freq` selector.
