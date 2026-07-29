---
title: FPGA resources
parent: Resource analysis
nav_order: 1
has_children: false
audience: python
summary: "What the counters mean — LUT, FF, DSP, BRAM, URAM, SRL — and how Vitis produces an area estimate after C-synthesis. The estimate comes from HLS's own binding decisions plus per-part characterization, before Vivado has optimized anything, which is why DSP and BRAM track well while LUT and FF are genuinely estimates."
---

# FPGA resources

An FPGA is a fixed grid of hard primitives. "Area" is how many of each a design consumes, and a design
fits only if **every** counter fits — running out of DSPs is as fatal as running out of LUTs, which is
why a resource model has to predict each of them rather than one aggregate number.

## The counters

Numbers below are for the **xc7z020** (Zynq-7020, Artix-7 fabric) used by the reference platform; the
kinds are general, the quantities are not.

| counter | what it is | on xc7z020 |
|---|---|---|
| **LUT** | Look-Up Table — the combinational primitive. A 6-input LUT implements any Boolean function of 6 inputs (or two 5-input functions sharing inputs). The general-purpose currency of the fabric. | 53,200 |
| **FF** | Flip-Flop — a 1-bit register, paired with the LUTs in each slice. Pipelining costs these. | 106,400 |
| **DSP** | A hard multiply-accumulate block (`DSP48E1` here): a 25×18 signed multiplier, a 48-bit accumulator, and a pre-adder. Arithmetic that fits belongs here; arithmetic that does not is built from LUTs and costs far more of them. | 220 |
| **BRAM** | Block RAM — hard dual-port memory, physically 36 Kb blocks usable as one 36 Kb or two independent 18 Kb. Vitis counts in **18 Kb units** (`BRAM_18K`). | 280 (= 140 × 36 Kb) |
| **URAM** | UltraRAM — larger (288 Kb) hard memory blocks, **UltraScale+ only**. Absent here, which is why the report shows `0` available. | 0 |
| **SRL** | Not a separate primitive: a LUT in a `SLICEM` configured as a shift register. Some tools count it separately because it is a LUT spent a particular way. | — |

Two consequences worth internalizing, because they drive most of what a resource model has to encode:

**Storage has several homes.** An array can become BRAM, distributed RAM in LUTs (LUTRAM), or plain
registers, and HLS chooses. Applying `ARRAY_PARTITION` typically pushes storage *out* of BRAM and into
LUTs and FFs — so a partitioning pragma can take BRAM to zero while LUT climbs. That is a
discontinuity, not a gradient.

**Arithmetic has a cliff and a packing win.** A multiply wider than the DSP's 25×18 needs more than one
DSP; a multiply much narrower may let the tool fit *two* multiplies into one. So DSP count is a step
function of bit width that moves in **both** directions — measured on the FIR example, an 8-bit design
used fewer DSPs than taps while a 24-bit design used twice as many.

## How Vitis produces the estimate

C-synthesis does not just translate C to RTL — it **schedules** operations into clock cycles and
**binds** each one to a physical resource. Every multiply is assigned to a DSP or to LUT logic; every
array is assigned to BRAM, LUTRAM, or registers; every operation gets a functional unit.

The area report is a direct consequence of those binding decisions, costed with per-part
characterization data. That is why the numbers appear the instant C-synthesis finishes, with no
place-and-route.

{: .warning }
> **The estimate precedes Vivado, and Vivado re-optimizes.** Logic synthesis and implementation share
> logic, absorb constants, retime registers, and map differently — including *across* module
> boundaries HLS treated as separate.
>
> The practical rule: **DSP and BRAM track well**, because they reflect explicit binding decisions HLS
> made and reported. **LUT and FF are genuinely estimates** and can differ substantially from
> post-implementation numbers. Lead with DSP and BRAM when the conclusion has to be firm; treat LUT/FF
> as indicative until a Vivado run says otherwise.

This is why the [record store](../calib/modules.md) tags every measurement with a `source` —
`hls_estimate`, `vivado_synth`, or `vivado_impl` — so an estimate can be upgraded later without
anything downstream having to change.

## Two report conventions

**`~0`** means *negligible but nonzero* — Vitis writes it in place of a number, so a naive sum of a
report column can hit a string. Waveflow normalizes it to `0` at the boundary
([`normalize_resources`](../calib/modules.md)).

**`AVAIL_*` and `UTIL_*` columns** describe the *device* and the percentage used, not this design's
consumption. They must not be summed — an `AVAIL_LUT` totalled across four modules looks like a
resource count and is nonsense. Waveflow drops them at the same boundary.

## See also

- [Reading the report](./parser.md) — getting these numbers into Python.
- [Composite kernels](./composite.md) — attributing them to the parts of a design.
