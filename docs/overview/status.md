---
title: Project status
parent: Overview
grand_parent: Waveflow
nav_order: 7
---

# Project status

Waveflow is **early-stage research software**. The foundation — the type system, the simulator, the build graph, and the codegen path to Vitis HLS — is built and works end to end, and is validated **bit-for-bit
against the real toolchain** on a growing set of individual modules. What does *not* exist yet is an
integrated, multi-module subsystem or a head-to-head comparison against existing frameworks. This page is an
honest snapshot of where that line currently sits.

## What works today

While the project is early, the **core of the Waveflow framework is developed and validated end to end**:

- A rich set of [data schemas](../guide/schema/)
are already supported.  These schemas 
include fixed and floating point [fields](../guide/schema/python/fields.md), and [data lists](../guide/schema/python/datalists.md), [data unions](../guide/schema/python/dataunion.md), [data arrays](../guide/schema/python/dataarrays.md) with full numpy [vectorization](../guide/vectorization/).
Users can auto-generate the corresponding Vitis code, verified to match the Python model bit-exact.
- Core [interfaces](../guide/interface/) such as HLS stream, AXI memory-mapped, and other memory interfaces are also in place.  Users can create [hardware components](../guide/flows/modules.md) with the interfaces, auto-generate Vitis HLS code implementing core transactions including block and pipelined reads of raw data and schemas.  These have also been verified as functionally correct and cycle approximate.  Hardware components support [parameterization](../guide/flows/parametrization.md).
- A SimPy-based discrete-event simulator (DES) has also been developed, enabling cycle-approximate timing of processing and interface transactions of the hardware components.
- A [build DAG](../guide/build/)
has been developed that provides pre-built steps for DES simulation, code generation, Vitis HLS simulation, RTL co-simulation and others.  Tools are available for timing and resource extraction and visualization enabling closed-loop functional and timing verification.  
- The framework has been validated **end to end**
on a set of simple [examples](../examples/) with individual kernels over a variety of interfaces.  The validation demonstrated bit-exact match and near exact timing match with proper calibration. 




## What is coming next?

Several new core components are on the horizon.

- **More complex modules**.  We will soon include modules wrapping GEMM and FFT blocks for Vitis L1 DSP.
- **Multi-module integrated systems.** Composing the individually-verified modules into working subsystems — the conjugate-gradient demonstrator below is the first.
- **AI integration.** The structured framework is the substrate AI needs; the code generation, agentic DSE, and MCP developer tooling are still being built out.
- **Model calibration tools.** Fitting the resource and cycle-timing models from measured data.
- **Comparative benchmarks.** Measuring Waveflow against PyMTL / Chisel / HLS on design productivity and result quality.
- **The full [SALSA](./salsa.md) machine** — the long-term target the whole foundation is building toward.

## The next milestone: a conjugate-gradient demonstrator

The deliberate next step is the first *system-level* result: **conjugate gradient (CG)** — a real
linear-solve kernel — built from **three interacting modules**:

- a **systolic array** (matrix–matrix product),
- the **VMAC** vector engine (the complex vector-MAC tile, already verified), and
- a **general-purpose processor** (control and scalar work).

Composed and simulated as one system, the goal is to show that Waveflow's **fast, bit-exact, timing-aware simulation** — paired with **agentic design-space exploration** — finds good parameterizations *much faster*
than an RTL-first flow. That is a concrete, publishable validation of the central claim, and the first
design that exercises the platform as an integrated whole rather than a set of verified parts.

## Trajectory

> verified foundation **(here)** → the CG demonstrator (next) → additional SALSA tiles → the full
> reconfigurable system

The bet is that getting the *foundation* right is the hard part; with a correct, verified substrate, the
convincing examples follow.
