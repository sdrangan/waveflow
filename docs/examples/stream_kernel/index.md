---
title: Free-running polynomial
parent: Examples
nav_order: 0.5
has_children: true
example_dir: examples/stream_kernel
summary: "The first example, and the shape most accelerators take: a free-running kernel that answers commands on streams. A polynomial evaluator reads a command (its id, a sample count, four coefficients) and its samples on one AXI4-Stream, writes the results on a second -- headed by which command and how many -- and a status on a third, the last command seen; a burst that ends early is fatal: the kernel reports it and stops. The page set walks the whole flow in order -- the protocol, the golden, the kernel and its hand-written HLS body, the system flow (csynth, the RTL run under XSI, the comparison with pysim), and declared timed phases calibrated in the kernel's own generated harness. The same kernel on a bus, behind AXI-MM with a software host, is its own example, The polynomial on a bus."
---
# Free-running polynomial

A polynomial evaluator, $y = c_0 + c_1 x + c_2 x^2 + c_3 x^3$ on float32 samples, as a **free-running
kernel**: no `ap_start`, no register map -- it fires once per command, and runs until an error stops
it. It is the first
example because its shape is the one most accelerators take: a command comes in on a stream, the work
happens on its data, and the answer goes out on streams, including the answer that something went
wrong. The arithmetic is deliberately trivial, so every page is about the flow.

The kernel is one Python class, [`PolyKernel`](../../../examples/stream_kernel/stream_kernel.py). Around
it, everything is generated or comes from the framework: its testbench (a harness built from its ports),
its RTL top, its calibration, and -- in [The polynomial on a bus](../stream_kernel_mm/index.md) -- its
memory-mapped device.

## Learning Objectives

In the order the pages teach it:

1. Define a [**command-response protocol**](../../guide/systems/command_response.md) for a simple
   kernel: a command and its data in, the results out headed by which command they answer and how
   many.
2. Add a **status stream** for error reporting, and halt the kernel on an error.
3. Write [**DataSchema** classes](../../guide/schema/python/) for every message to and from the kernel.
4. Write a **bit-exact golden model** as pure Python functions, and compute the expected outputs from
   intent.
5. Write the kernel as a [`FreeRunMod`](../../guide/flows/concurrent_python.md) with **framed stream
   ports**, one firing per command: read the command, head the results, run the samples through a
   pipelined phase, write the status, and stop after an error.
6. Write its **HLS twin** with the generated lane routines
   ([loop optimization](../../guide/vectorization/hls/loop_optimization.md)), and generate its top.
7. Make the kernel the DUT of its own [**harness**](../../guide/flows/units.md), and run the system
   flow: csynth, the RTL run under XSI, and the comparison of RTL against pysim.
8. Declare the kernel's timing as [**timed phases**](../../guide/timing_model/timed_phases.md) and
   calibrate them in its harness.

Putting the same kernel on a bus -- behind AXI-MM, with a software host, its timing composed from the
calibrated parts -- is its own example, [The polynomial on a bus](../stream_kernel_mm/index.md).

## The pages

- [The protocol](protocol.md) -- three streams, one command at a time, a status plane, errors fatal.
- [The golden](golden.md) -- the schemas, the reference arithmetic, stimulus and expected outputs.
- [The kernel](kernel.md) -- `PolyKernel.run_iter`, its HLS body, and the generated top.
- [The system flow](system_flow.md) -- the kernel's harness, csynth, the RTL run, the comparison.
- [Timing](timing.md) -- two timed phases, calibrated in the harness and filed under the kernel's
  identity.

For the general practice behind the last two -- harnesses, stand-ins, calibrating a unit alone and
composing a parent -- see [Developing module by module](../../guide/flows/units.md).

## Running it

```bash
python -m examples.stream_kernel.stream_kernel_build --through pysim          # pysim only
python -m examples.stream_kernel.stream_kernel_build                          # through compare (Vitis, Vivado)
python -m examples.stream_kernel.stream_kernel_build --through calib_phases   # calibrate the timing
```

The gate is [`tests/examples/test_stream_kernel.py`](../../../tests/examples/test_stream_kernel.py). Cycle
counts on these pages were measured with Vitis HLS and Vivado 2025.1 at 100 MHz.

This kernel is the free-running form of [Streaming polynomial](../stream_inband/index.md), which
teaches the same arithmetic as a host-activated kernel with a status register.
