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

## Learning Objectives

In going through this example, you will learn to:

- Model hardware as [**free-running component** classes](../../guide/flows/concurrent.md) (the `FreeRunComp` class in Waveflow), and interconnect them into **composite free-running components** to describe a target hardware object
- Develop a **concurrent testbench** as a composite graph that wires the DUT to stimulus and capture, using Waveflow's built-in stream source/sink models (`CmdDriver`, `WordSink`)
- Run a Python **concurrent simulation** of the composite target hardware in conjunction with the testbench components
- Generate a **concurrent Vitis C++ kernel** from the Python descriptions, where each sub-component becomes an **HLS task**
- Synthesize the concurrent Vitis C++ kernel into RTL with **Vitis HLS C-synthesis** (the generated `mem_copy.tcl`)
- Map each testbench component to an **XSI BFM model** (hand-written framework classes) and **generate the XSI harness** that wires those models to the RTL and drives the clock
- Run the XSI simulation to extract timing and functionally validate the generated RTL

## In this example

Start with [the module overview](./memcpy.md) — the three-stage design and how it is wired. The pages
then build it up from Python, parallel to the [register-map example](../regmap/): the model, the
generated `ap_ctrl_none` kernel, the XSI testbench, and the RTL/XSI simulation.
