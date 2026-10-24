---
title: The Waveflow flow
parent: Overview
grand_parent: Waveflow
nav_order: 3
---

# The Waveflow flow

In Waveflow, everything *flows* from the underlying [Python model specification](./pymodel.md). The
design under test (the **DUT**) is simulated at **two levels**
([The DUT and two levels of simulation](../guide/systems/dut.md)):

- **pysim** — a **SimPy discrete-event simulation (DES)** runs it **fast** and **bit-exact**, using a
  **calibrated, cycle-approximate** timing model, beside anything else the question needs (a host, a
  wireless channel, a processor model);
- **RTL simulation** — the *same* model generates the **HLS** top, message structs and lane routines
  around the kernel's HLS body, and an `HwSystem(sim, dut)` runs the synthesized RTL cycle by cycle under
  Vivado's simulator, with every testbench part as its C++ twin: cycle- and resource-**exact** ground
  truth.

The flow, a page per step, is the guide's [Flow Overview](../guide/systems/index.md).

<p align="center">
  <img src="../assets/waveflow_methodology.svg" width="820"
       alt="Two-loop methodology: a violet build pipeline (Python model, SimPy DES, HLS codegen, RTL synth/sim) with a teal feedback system — Agentic DSE and Functional verification below, Calibration and the Timing/resource model above, and AI assisting codegen and exploration.">
</p>

## The build pipeline

The whole pipeline is orchestrated by a **`BuildDag`** — a directed graph of build steps (generate
sources, simulate, synthesize, compare bits) with explicit dependencies between them. Because those
dependencies are declared, a run is **deterministic and reproducible**: each step's inputs and
outputs are tracked, stages re-run only when their inputs change, and every artifact — generated
kernels, simulation results, timing and resource reports — traces back to the design that produced
it.

## Inner loop — fast, all-Python (agentic DSE)

The SimPy DES produces performance — accuracy, throughput, resources — that drives **design-space
exploration**. This loop is cheap enough to sweep bit widths, queue sizes, memory organization,
and iteration counts, so an **agent** can explore many designs per toolchain run. It closes by
refining the Python model's architecture and parameters. Sweeps and the models that price a point are
built; the exploration loop itself is not yet a framework tool
([Design-space exploration](../guide/systems/dse.md)).

## Outer loop — slow, sparse (calibration)

The same Python model generates the HLS top and the RTL testbench; csynth and the RTL simulation give
cycle/resource-**exact** numbers, and **calibration** fits the **timing model** the inner loop relies
on — each module in its own harness against its own RTL, a system's timing composed from its modules'
records with nothing fitted to it ([Timing and resources](../guide/systems/calibration.md)). A few sparse
toolchain runs keep the fast models trustworthy — the model is the **bridge** the two loops share.

## Functional verification

In parallel, **functional verification** is a chain of comparisons: the **golden** (pure Python
functions) against the spec's worked examples, **pysim** against the golden, and the **RTL** against
pysim, word for word ([Functional verification](../guide/systems/verification.md)). A mismatch is a model
or HLS bug to fix — this is the correctness guarantee that makes the fast loop's numbers worth trusting.
