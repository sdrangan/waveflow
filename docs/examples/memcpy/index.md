---
title: Free-running memory copy
parent: Examples
nav_order: 5
has_children: true
example_dir: examples/mem_copy
summary: "The first kernel that masters memory: a composite of free-running hls::tasks copying words between memory regions, which Vitis cannot co-simulate. So it is stated as an HwSystem, and the system flow generates a top and a harness from it and drives the real RTL cycle by cycle through XSI. It also reuses the MemRStream / MemWStream adaptors, whose timing models ship pre-fit, so the loosely-timed pysim reproduces the RTL with no calibration of its own."
---
# Memory Copy — a free-running composite kernel

This is the worked example for the [concurrent (free-running) flow](../../guide/systems/index.md),
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

What this adds to the examples before it -- a kernel that only answered streams, alone
([Free-running polynomial](../stream_kernel/index.md#learning-objectives)), in stages
([Block equalizer](../block_eq/index.md#learning-objectives)) or behind a bus
([The polynomial on a bus](../stream_kernel_mm/index.md#learning-objectives)) -- in the order the pages
teach it:

1. Make a kernel **master memory**: its reads and writes go over two `m_axi` bundles through the
   framework's [`MemRStream` / `MemWStream`](../../guide/interface/axi_mm/master.md) adaptors, with no memory logic of its
   own.
2. **Forward each command in-band** along the pipeline -- one stream, each command welded to its data
   -- so every stage knows which job it is working on.
3. Put a **memory model** (`MemoryMod`, one shared arena) in the testbench, and check the golden on the
   words the RTL wrote into it.
4. Read the generated `hls::task` top's `m_axi` ports (`offset=off` and `stable`), and state the
   system as a [`GraphSystem`](../../guide/build/xsi_system.md#graph-system), the older form of
   `HwSystem(sim, dut)`: the kernel the DUT, the drivers and the memory the testbench.
5. Draw the concurrent timing of a traced RTL run on an [**activity diagram**](./timing.md): the
   pipeline overlap and where every cycle of the period goes.
6. Reuse the platform's **pre-fit timing** of the `m_axi` bus and the memory-stream adaptors, so pysim
   matches the RTL's period with no calibration of your own.

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
