---
title: Platform identity
parent: Platforms
nav_order: 1
audience: python
api: [Platform, PlatformCalib, BuildConfig]
summary: "What a platform IS, and what identifies one. A platform holds the timing and resource models valid for a single target, and is identified by an FPGA part, a synthesis clock, the memory system, and the resource counters that technology is measured in. Explains why each matters: the part fixes primitive latencies, the synthesis clock fixes the HLS schedule (and is not the sim clock), the memory system fixes the bus law and is not in the part number, and the counters are themselves properties of the device. The mechanics of creating one and confirming a build against it are in Creating a platform."
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

**The default is the Vitis/FPGA set** — `lut`, `ff`, `dsp`, `bram`, `uram`, `srl`. A platform that omits
`res_types` is measured in those, which is every FPGA platform including the shipped one, so the key is
absent from an ordinary manifest rather than spelled out in it.

It belongs in the identity because a counter set is exactly as technology-specific as the part: an ASIC
flow counts cell area and macro instances, in a *float* rather than a count. This is the seam a
non-FPGA technology enters through — declare a platform, rather than reworking the model layer — and it
is what lets a model's counter names be **validated** rather than merely conventional: naming a counter
the platform does not measure in raises instead of being silently dropped when counters are summed.

## See also

- [Creating a platform](./create.md) — making one for a project, and how naming one on a build confirms
  it against this stored identity.
- [Directory layout](./layout.md) — the library half: what is stored under an identity, and how it is keyed.
- [The bus-transfer model](../calib/bus_model.md) — the platform-scoped `m_axi` law that lives beside the
  manifest.
- [Component residuals](../calib/component_residual.md) — the per-`(component, platform)` fits under
  `components/`.
