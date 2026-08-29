---
title: The Waveflow flow
parent: Overview
grand_parent: Waveflow
nav_order: 3
---

# The Waveflow flow

In Waveflow, everything *flows* from the underlying [Python model specification](./pymodel.md). A
**SimPy discrete-event simulation (DES)** runs it **fast** and **bit-exact** using a **calibrated,
cycle-approximate** timing/resource model; the *same* model generates **HLS codegen** (kernel +
testbench), which **RTL synthesis / simulation** turns into cycle- and resource-**exact** ground truth.

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
refining the Python model's architecture and parameters.

## Outer loop — slow, sparse (calibration)

The same Python model generates HLS; C-sim, co-sim, and synthesis give cycle/resource-**exact**
numbers, and **Calibration** fits the **timing/resource model** the inner loop relies on. A few
sparse toolchain runs keep the fast models trustworthy — the model is the **bridge** the two
loops share.

## Functional verification

In parallel, **functional verification** compares the **SimPy golden** against the generated
**RTL**, **bit-for-bit**. A mismatch is a model or codegen bug to fix — this is the correctness
guarantee that makes the fast loop's numbers worth trusting.
