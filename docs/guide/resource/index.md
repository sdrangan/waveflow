---
title: Resource Analysis Tools
parent: Guide
nav_order: 11.5
has_children: true
audience: python
api: [CsynthParser, attribute_resources, report_from_solution, SynthReport]
summary: "The measurement side of FPGA resource utilization: what an FPGA's resources are, how Vitis estimates them after C-synthesis, how to read that report with CsynthParser, and how to decompose a composite kernel's total into the modules and the interface logic that caused it. This section extracts the numbers; Resource models predict them, and Model calibration stores and fits them."
---

# Resource Analysis Tools

Any hardware module occupies **resources** — physical components of the silicon it is built from. An
ASIC flow measures these as **area** and **power**. Waveflow's initial focus is FPGA flows, specifically
Xilinx / AMD parts, where a design's **resource utilization** is instead counted in the fabric's fixed
primitives: LUTs, flip-flops, DSPs and memory blocks.

Whatever the target, hardware design inevitably trades resource consumption against the other design
metrics — throughput, accuracy, features. Making that trade deliberately requires knowing what a module
actually costs, measured as part of the design flow rather than discovered at the end of it.

This section describes Waveflow's tools for measuring real utilization from Vitis synthesis on FPGAs.
Tools for other flows, including ASIC, may follow.

## In this section

- [FPGA resources](./xilinx.md) — what a LUT, FF, DSP, BRAM and URAM actually are, and how Vitis
  produces a utilization estimate after C-synthesis (including what that estimate is and is not good
  for).
- [Reading the report — `CsynthParser`](./parser.md) — the parser over `csynth.xml`: totals, the
  per-module breakdown, and the per-loop pipeline table.
- [Composite kernels](./composite.md) — decomposing a multi-task kernel's total into its modules and
  the interface logic between them, and the two traps that otherwise corrupt the arithmetic.

## See also

- [Model calibration](../calib/) — storing resource measurements per module so a later design reuses
  them, and the [record store](../calib/modules.md) that keys them.
- [Vitis build primitives](../build/vitis.md) — invoking C-synthesis from a build DAG.
- [Resource models](../resource_model/) — *predicting* utilization at a configuration you have not
  synthesized, from a handful of measurements taken here.
