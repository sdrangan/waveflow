---
title: Free-running memory copy
parent: Examples
nav_order: 5
has_children: true
example_dir: examples/mem_copy
summary: "The worked example for the free-running flow, the counterpart to the register-map one, and the first system: a composite of free-running hls::tasks copying words between memory regions, which Vitis cannot co-simulate. So it is stated as an HwSystem, and the system flow generates a top and a harness from it and drives the real RTL cycle by cycle through XSI. It also reuses the MemRStream / MemWStream adaptors, whose timing models ship pre-fit, so the loosely-timed pysim reproduces the RTL with no calibration of its own."
---
# Memory Copy — a free-running composite kernel

This is the worked example for the [concurrent (free-running) flow](../../guide/flows/concurrent.md),
the counterpart to the sequential [register-map example](../regmap/). Where `simp_fun` is a single
host-launched function, `mem_copy` is a **composite of free-running `hls::task`s** that copies a run of
words from one memory region to another — and it is verified not by Vitis co-simulation (which cannot
drive a free-running kernel) but by driving the real RTL cycle-by-cycle through an **XSI BFM**.

Beyond demonstrating composite free-running kernels, `mem_copy` **reuses core infrastructure** — the
reusable `MemRStream` / `MemWStream` adaptors and the `m_axi` bus — whose timing models Waveflow **ships
pre-fit** for supported platforms, so the loosely-timed pysim reproduces the RTL with **no calibration of
its own**. (Those shared models were in fact measured *on* `mem_copy` behind the scenes; that machinery
is the general [calibration guide](../../guide/calib/), not a step in this example — fitting a *custom*
component's timing is taken up in the interleaver example.) The final page visualizes that timing.

## Learning Objectives

In going through this example, you will learn to:

- Model hardware as [**free-running component** classes](../../guide/flows/concurrent.md) (the `FreeRunMod` class in Waveflow), and interconnect them into **composite free-running components** to describe a target hardware object
- Develop a **concurrent testbench** as a composite graph that wires the DUT to stimulus and capture, using Waveflow's built-in stream source/sink models (`StreamDriver`, `StreamSink`)
- Run a Python **concurrent simulation** of the composite target hardware in conjunction with the testbench components
- Generate a **concurrent Vitis C++ kernel** from the Python descriptions, where each sub-component becomes an **HLS task**
- Describe a **`GraphSystem`** that cuts the graph: the generated kernel as the **DUT**, and the
  stream driver, stream sink and memory around it as the **testbench**
- Synthesize the DUT into RTL with **Vitis HLS C-synthesis** (the generated `mem_copy.tcl`)
- Generate an **XSI harness** from the graph system: a Verilog top around the synthesized kernel,
  and a C++ testbench of each participant's BFM model answering its ports
- Simulate the RTL through the XSI harness, compare what the testbench recorded with pysim word for
  word, and check the golden and the cycle count on the RTL's recordings
- Visualize the concurrent timing of the components on an [**activity diagram**](./timing.md): the
  pipeline overlap and where every cycle of the period goes
- Reuse a pre-fit **platform timing model** (the `m_axi` bus and the memory-stream adaptors, shipped
  with Waveflow) so the loosely-timed pysim matches the RTL's period with no calibration of your own

## In this example

The pages build the design up from Python, parallel to the [register-map example](../regmap/). You
model it, test it in Python, generate the kernel, state the system, then run the RTL:

1. [Module overview](./memcpy.md) — the three-stage design, the in-band forwarding protocol, and how
   it is wired.
2. [Python model](./python.md) — the schemas and the three `FreeRunMod` leaves + the composite.
3. [Testbench (Python)](./testbench.md) — the `MemCopyTB` graph, the `MemCopySim` procedure, and
   running the pysim step.
4. [DUT codegen](./codegen_dut.md) — what an `hls::task` is, and how the design graph becomes the
   `ap_ctrl_none` top.
5. [The system](./system.md) — `MemCopySystem`, the cut, and what the system flow derives from it:
   the system top, the harness of BFM models, the scenario.
6. [RTL simulation](./rtlsim.md) — running the system flow end to end, the RTL-vs-pysim comparison,
   and the golden at 2908 cycles.
7. [Visualizing timing](./timing.md) — tracing an RTL run and rendering it: the pipeline overlap, the
   bottleneck, where every cycle of the period goes, and why the loosely-timed pysim reproduces it using
   the platform's shipped infra models (no per-example calibration).
