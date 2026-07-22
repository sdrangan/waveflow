---
title: Memory Copy 
parent: Examples
nav_order: 3
has_children: true
---
# Memory Copy — a free-running composite kernel

This is the worked example for the [concurrent (free-running) flow](../../guide/flows/concurrent.md),
the counterpart to the sequential [register-map example](../regmap/). Where `simp_fun` is a single
host-launched function, `mem_copy` is a **composite of free-running `hls::task`s** that copies a run of
words from one memory region to another — and it is verified not by Vitis co-simulation (which cannot
drive a free-running kernel) but by driving the real RTL cycle-by-cycle through an **XSI BFM**.

Beyond demonstrating composite free-running kernels, `mem_copy` doubles as a **calibration vehicle** for
core infrastructure: it composes the reusable `MemRStream` / `MemWStream` adaptors and the `m_axi` bus,
so the sweep that fits *its* timing produces the shared bus and mem-stream models that **every**
accelerator on the platform reuses. The final three pages are that arc.

## Learning Objectives

In going through this example, you will learn to:

- Model hardware as [**free-running component** classes](../../guide/flows/concurrent.md) (the `FreeRunComp` class in Waveflow), and interconnect them into **composite free-running components** to describe a target hardware object
- Develop a **concurrent testbench** as a composite graph that wires the DUT to stimulus and capture, using Waveflow's built-in stream source/sink models (`StreamDriver`, `StreamSink`)
- Run a Python **concurrent simulation** of the composite target hardware in conjunction with the testbench components
- Generate a **concurrent Vitis C++ kernel** from the Python descriptions, where each sub-component becomes an **HLS task**
- Synthesize the concurrent Vitis C++ kernel into RTL with **Vitis HLS C-synthesis** (the generated `mem_copy.tcl`)
- Map each testbench component to an **XSI BFM model** (hand-written framework classes) and **generate the XSI harness** that wires those models to the RTL and drives the clock
- Run the XSI simulation to extract timing and functionally validate the generated RTL
- Visualize the concurrent timing of the components on an [**activity diagram**](./timing.md) — the
  pipeline overlap and where every cycle of the period goes
- Insert [**timing models**](./timing_model.md) for the platform's shared infra components — the
  `m_axi` bus and the memory-stream adaptors — so the loosely-timed pysim reproduces the RTL
- Fit those models with a [**parameter sweep**](./timing_fitting.md) and store the fitted parameters in
  the platform library, so other accelerators on the same platform reuse them without re-calibrating

## In this example

The pages build the design up from Python, parallel to the [register-map example](../regmap/) — model
it, test it in Python, generate the kernel, generate the testbench, then run the RTL:

1. [Module overview](./memcpy.md) — the three-stage design, the in-band forwarding protocol, and how
   it is wired.
2. [Python model](./python.md) — the schemas and the three `FreeRunComp` leaves + the composite.
3. [Testbench (Python)](./testbench.md) — the `MemCopyTB` graph, the `MemCopySim` procedure, and
   running the pysim step.
4. [DUT codegen](./codegen_dut.md) — what an `hls::task` is, and how the design graph becomes the
   `ap_ctrl_none` top.
5. [Testbench codegen](./codegen_tb.md) — what XSI and a harness are, and how the *testbench* graph
   becomes the BFM harness.
6. [RTL simulation](./rtlsim.md) — running the RTL through XSI, inspecting the results, and comparing
   the timing to pysim.
7. [Visualizing timing](./timing.md) — tracing an RTL run and rendering it: the pipeline overlap, the
   bottleneck, and where every cycle of the period goes.
8. [Timing models](./timing_model.md) — the two models (bus + mem-stream) behind those numbers, and how
   they plug into the components.
9. [Fitting the models](./timing_fitting.md) — the sweep, the fit results, and the platform library that
   stores them so other accelerators reuse them.
