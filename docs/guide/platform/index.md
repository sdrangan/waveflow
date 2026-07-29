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

A **platform** is the target a design is built and measured against. It has two halves:

- **identity** — an FPGA part, a synthesis clock, and the resource counters that technology is
  measured in.
- **library** — everything fit or measured for that target: the bus-transfer law, timing residuals,
  and area records.

It is the *pairing* that makes it work. A cycle count or a LUT count is only meaningful for a
particular part at a particular clock, so the numbers and the identity they were taken under are
stored together and confirmed on use.

Waveflow ships one reference platform, so an installed user inherits working measurements for the
framework's own modules with no toolchain run at all; a project then
[creates its own](./create.md) seeded from it.

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

## In this section

- [Creating a platform](./create.md) — setting one up for a project: create, seed from upstream, wire
  into the build, and the `.gitignore` line that is easy to omit.
- [Directory layout](./layout.md) — what is in a platform, which tier it lives in, what is tracked.
- [Platform identity](./identity.md) — the manifest, the mismatch gate, and why cycle counts are only
  valid for the part they were fit against.
- [Managing a platform](./workflow.md) — the `waveflow_calib` commands, and the two-tier
  work → publish flow that is the only writer of a tracked library.

## See also

- [Model calibration](../calib/) — how the fits stored here are produced.
- [`BuildConfig`](../build/corecomp.md) — the `platform` / `part` / `clk_freq` selector.
