---
title: Project status
parent: Overview
grand_parent: Waveflow
nav_order: 7
---

# Project status

Waveflow is **early-stage research software**. The foundation — the type system, the simulator, the build graph, and the codegen path to Vitis HLS — is built and works end to end, and is validated **bit-for-bit
against the real toolchain** on a growing set of modules and on the first multi-module systems. What does
*not* exist yet is a design on a board or a head-to-head comparison against existing frameworks. This page is an
honest snapshot of where that line currently sits.

## What works today

While the project is early, the **core of the Waveflow framework is developed and validated end to end**:

- A rich set of [data schemas](../guide/schema/)
are already supported.  These schemas 
include fixed and floating point [fields](../guide/schema/python/fields.md), and [data lists](../guide/schema/python/datalists.md), [data unions](../guide/schema/python/dataunion.md), [data arrays](../guide/schema/python/dataarrays.md) with full numpy [vectorization](../guide/vectorization/).
Users can auto-generate the corresponding Vitis code, verified to match the Python model bit-exact.
- Core [interfaces](../guide/interface/) such as HLS stream, AXI memory-mapped, and other memory interfaces are also in place.  Users can create [hardware modules](../guide/flows/modules.md) with the interfaces, auto-generate Vitis HLS code implementing core transactions including block and pipelined reads of raw data and schemas.  These have also been verified as functionally correct and cycle approximate.  Hardware modules support [parameterization](../guide/flows/parametrization.md).
- A SimPy-based discrete-event simulator (DES) has also been developed, enabling cycle-approximate timing of processing and interface transactions of the hardware modules.
- A [build DAG](../guide/build/)
has been developed that provides pre-built steps for the [system flow](../guide/systems/index.md): DES simulation, code generation, csynth, RTL simulation under Vivado's simulator (XSI), and the comparison of the RTL against the DES.  Tools are available for timing and resource extraction, [calibration](../guide/systems/calibration.md) and visualization enabling closed-loop functional and timing verification.  (The older Vitis C-simulation / co-simulation path is kept under [Legacy flows](../guide/legacy/index.md).)
- The framework has been validated **end to end**
on a set of [examples](../examples/), from individual kernels over a variety of interfaces to multi-module systems: a three-stage pipeline ([block_eq](../examples/block_eq/index.md)), two kernels, a memory and a host on a bus ([markov](../examples/markov/index.md)), and a CG detector composed from two linear-algebra modules ([mimo_cg](../examples/mimo_cg/index.md)).  The validation demonstrated bit-exact match and near exact timing match with proper calibration ([Timing and resources](../guide/systems/calibration.md)).
- An [MCP server](../guide/ai_tooling/index.md) gives an AI assistant the guide, the reference examples and a process per system shape (experimental).




## What is coming next?

Several new core modules and capabilities are on the horizon.

- **More complex modules**.  The Vitis L1 SSR FFT is already wrapped as a module ([vitis_fft](../examples/vitis_fft/index.md), with Waveflow's own [SSR FFT](../examples/ssr_fft/index.md) beside it); more Vitis L1 DSP blocks will follow.
- **Larger integrated systems.** Composing the individually-verified modules into working subsystems — [block_eq](../examples/block_eq/index.md), [markov](../examples/markov/index.md) and the conjugate-gradient demonstrator below are the first.
- **AI integration.** The structured framework is the substrate AI needs; the [MCP developer tooling](../guide/ai_tooling/index.md) is built (experimental), while agentic DSE — the exploration loop as a framework tool ([Design-space exploration](../guide/systems/dse.md)) — is still being built out.
- **Model calibration tools.** Timing and resource models are already fitted from measured data, per module, and composed ([Model calibration](../guide/calib/index.md)); more of the system — such as memory traffic under contention — remains to be modeled.
- **Comparative benchmarks.** Measuring Waveflow against PyMTL / Chisel / HLS on design productivity and result quality (simulation speed is measured first, in [Evaluation](../evaluation/index.md)).
- **The board.** IP packaging and a validated block design are built; the processor in the block design, the board and the bitstream come next ([Toward the board](../guide/flows/bitstream_ipi.md)).
- **The full [SALSA](./salsa.md) machine** — the long-term target the whole foundation is building toward.

## The first system-level milestone: a conjugate-gradient demonstrator

The first *system-level* result is **conjugate gradient (CG)** — a real linear-solve kernel, as an
MMSE detector for the massive-MIMO uplink ([mimo_cg](../examples/mimo_cg/index.md)) — built from
Waveflow's two [linear-algebra modules](../guide/linalg/index.md):

- a **systolic array** (matrix–matrix product), and
- a **CG vector unit** (the per-iteration vector work).

Composed as one free-running detector, it matches its bit-exact Python model at RTL at K = 4, 8 and 16
users and every iteration count, and its design-space study priced points with calibrated cost models and
checked its exploration against a 1,440-build brute force (Vitis 2024.1). The goal is to show that
Waveflow's **fast, bit-exact, timing-aware simulation** — paired with **agentic design-space exploration** —
finds good parameterizations *much faster* than an RTL-first flow. That is a concrete, publishable
validation of the central claim, and the first design that exercises the platform as an integrated whole
rather than a set of verified parts. Still to come: a **general-purpose processor** (control and scalar
work) in the same system — modelled and calibrated today as the
[processor model](../guide/cpu/index.md), a Cortex-A53 measured on gem5 — and the exploration loop as a
framework tool rather than the example's own.

## Trajectory

> verified foundation → the CG demonstrator **(here)** → additional SALSA tiles → the full
> reconfigurable system

The bet is that getting the *foundation* right is the hard part; with a correct, verified substrate, the
convincing examples follow.
