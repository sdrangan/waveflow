---
title: Resource analysis
parent: Guide
nav_order: 13.5
has_children: true
audience: python
api: [CsynthParser, attribute_resources, report_from_solution, SynthReport]
summary: "The measurement side of FPGA area: what an FPGA's resources are, how Vitis estimates them after C-synthesis, how to read that report with CsynthParser, and how to decompose a composite kernel's total into the modules and the interface logic that caused it. The counterpart to Timing Analysis Tools — this section extracts the numbers; Model calibration stores and fits them."
---

# Resource analysis

A design's **area** — how many LUTs, flip-flops, DSPs and memory blocks it occupies — is the constraint
that decides whether an accelerator fits, and one of the two axes (with throughput) that any design
exploration trades against accuracy.

This section is the **measurement** side of that: getting trustworthy area numbers out of the toolchain
and attributing them to the parts of your design that caused them.

## Where this sits

The guide separates these concerns by *role*, and resources follow the same split as timing:

| role | timing | resources |
|---|---|---|
| **measure** | [Timing Analysis Tools](../timing/) — VCD, traces, diagrams | **this section** — the synthesis report |
| **model** | [Timing Models](../timing_model/) | *(not yet built)* |
| **fit / store** | [Model calibration](../calib/) | [Model calibration](../calib/) |

The distinction that matters most is between *measuring* and *predicting*. Everything here is
measurement: the number comes from a report about hardware that was actually synthesized. Predicting
area at a point you have **not** synthesized is a separate problem, and lives with the
[calibration machinery](../calib/modules.md) that stores measurements so a later design can reuse them
instead of paying for another synthesis.

## In this section

- [FPGA resources](./xilinx.md) — what a LUT, FF, DSP, BRAM and URAM actually are, and how Vitis
  produces an area estimate after C-synthesis (including what that estimate is and is not good for).
- [Reading the report — `CsynthParser`](./parser.md) — the parser over `csynth.xml`: totals, the
  per-module breakdown, and the per-loop pipeline table.
- [Composite kernels](./composite.md) — decomposing a multi-task kernel's total into its modules and
  the interface logic between them, and the two traps that otherwise corrupt the arithmetic.

## See also

- [Model calibration](../calib/) — storing resource measurements per module so a later design reuses
  them, and the [record store](../calib/modules.md) that keys them.
- [Vitis build primitives](../build/vitis.md) — invoking C-synthesis from a build DAG.
