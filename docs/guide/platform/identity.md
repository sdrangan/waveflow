---
title: Platform identity
parent: Platforms
nav_order: 1
audience: python
api: [Platform, VITIS_RES_TYPES]
summary: "What identifies a platform, in two parts. THE TARGET — an FPGA part, a synthesis clock and the memory system — decides what a cycle count means: the part fixes primitive latencies, the synthesis clock fixes the HLS schedule (and is not the sim clock), and the memory system fixes the bus law without appearing in the part number. THE RESOURCE TYPES decide what an area number even is; they default to the FPGA counters and are declared per platform, which is the seam a non-FPGA technology enters through and what makes counter names validated rather than conventional."
---

# Platform identity

A measurement is only valid for the hardware it was taken on. A **platform** is that hardware, named:
it holds every timing and resource model valid for one target, and carries the identity of that target
so nothing can use those models against a different one.

Two things make up that identity:

- **the target** — an FPGA **part**, a **synthesis clock**, and in practice the **memory system**, which
  the part number does not capture. These decide what a *cycle count* means.
- **the resource types** — the counters this technology is measured in. These decide what an *area
  number* even is.

Both are stored in the platform, and both are checked when a build uses it. What is *stored under* that
identity — the fitted residuals and measured records — is [Directory layout](./layout.md).

## The target: part, clock, and memory

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
why the counter vocabulary is part of the identity too (see [the resource types](#the-resource-types)).

Because two of these (part, clock) fix the kernel cycles, one (memory) fixes the bus law, and the
technology fixes what is even being counted, a platform is **human-named** with a manifest, not a bare
part-number directory — a raw part string collides across clock targets and cannot express the memory
system.

### The two clocks

There are two clocks, and only one of them changes the numbers:

| Clock                             | Role                                            | Effect on cycles                                                                                      |
| --------------------------------- | ----------------------------------------------- | ----------------------------------------------------------------------------------------------------- |
| sim `Clock(freq=…)`             | cycles ↔ seconds converter at the sim boundary | none — residuals are stored in **cycles**, so a re-deploy at a new sim frequency needs no refit |
| synthesis `create_clock -period` | the target HLS schedules to                     | **changes the schedule → changes the cycles**                                                  |

So the platform key needs the **synthesis** clock. `Platform.synth_period_ns` is exactly the
`create_clock -period` the TCL emits (`1e9 / clk_freq`).

## The resource types

A resource type is one **counter** an area measurement is expressed in — how many LUTs, how many DSPs,
how much cell area. A design fits only if *every* counter fits, which is why area is never a single
number: running out of DSPs is as fatal as running out of LUTs.

Which counters exist is a property of the **technology**, not of the design. That is why they are part
of a platform's identity rather than a global constant.

### The default: the FPGA set

In Waveflow, the current default resource types are based on the [Xilinx/AMD FPGA resources](../resource/xilinx.md), namely:

```text
lut   ff   dsp   bram   uram   srl
```

The [FPGA resources page](../resource/xilinx.md)
explains what each counter actually is, how far to trust an HLS estimate of it, and the two report
conventions (`~0`, and the `AVAIL_`/`UTIL_` columns) that bite if you sum them naively.

### Declaring a different set

In the future, Waveflow may target other FPGA families or even ASICs.  In this case, other resources will likely be needed, such as area.
In order to support this future capability, a platform on another technology can declare its own when it is
[created](./create.md#creating-the-directory):

```bash
waveflow_calib new calib/platforms/asic45 --part tsmc45 --clk 1e9 \
                   --res-types cell_area macros regs
```

For example, an ASIC flow can count cell area and macro instances, in a *float* rather than a count — nothing like a
LUT. This is the seam such a flow enters through: declare a platform, rather than reworking the model
layer.

{: .note }
> Declaring the vocabulary is what makes counter names **validated** rather than merely conventional. A
> model naming a counter the platform does not measure in raises immediately — where previously a typo
> predicted fine on its own and was silently dropped when counters were summed, so the module
> contributed **zero** and the design read as cheaper than it is.

## See also

- [Creating a platform](./create.md) — making one for a project, and how naming one on a build confirms
  it against this stored identity.
- [Directory layout](./layout.md) — the manifest this identity is written to, and everything stored
  under it.
- [FPGA resources](../resource/xilinx.md) — what the default counters are and how far an HLS estimate of
  them can be trusted.
- [The bus-transfer model](../calib/bus_model.md) — the platform-scoped `m_axi` law that lives beside the
  manifest.
- [Component residuals](../calib/component_residual.md) — the per-`(component, platform)` fits under
  `components/`.
