---
title: Free-running polynomial
parent: Examples
nav_order: 0.5
has_children: true
example_dir: examples/stream_kernel
summary: "The first example, and the shape most accelerators take: a free-running kernel that answers commands on streams. A polynomial evaluator reads a command (its id, a sample count, four coefficients) and its samples on one AXI4-Stream, writes the results on a second and a response on a third; a burst that ends early is answered, never halted on. The page set walks the whole flow in order -- the protocol, the golden, the kernel and its hand-written HLS body, the system flow (csynth, the RTL run under XSI, the comparison with pysim), declared timed phases calibrated in the kernel's own generated harness, and finally the same kernel behind AXI-MM with a two-thread software host, whose timing is composed from the calibrated parts with nothing fitted and lands within 1% of its RTL."
---

# Free-running polynomial

A polynomial evaluator, $y = c_0 + c_1 x + c_2 x^2 + c_3 x^3$ on float32 samples, as a **free-running
kernel**: no `ap_start`, no register map -- it fires once per command and runs forever. It is the first
example because its shape is the one most accelerators take: a command comes in on a stream, the work
happens on its data, and the answer goes out on streams, including the answer that something went
wrong. The arithmetic is deliberately trivial, so every page is about the flow.

The kernel is one Python class, [`PolyKernel`](../../../examples/stream_kernel/stream_kernel.py). Around
it, everything is generated or comes from the framework: its testbench (a harness built from its ports),
its RTL top, its calibration, and later its memory-mapped device.

## What you will be able to do

In the order the pages teach it:

1. State a kernel's command-response protocol: what crosses each stream, and how an error is answered.
2. Write a bit-exact golden as pure functions, and compute the expected outputs from intent.
3. Write the kernel's Python behavior and its HLS twin, one firing per command, and generate its top.
4. Run the system flow: csynth, the RTL run under XSI, and the comparison of RTL against pysim.
5. Declare the kernel's timing as timed phases and calibrate them in its generated harness.
6. Put the same kernel behind AXI-MM with a software host, and check that its timing composes from the
   calibrated parts with nothing fitted.

## The pages

- [The protocol](protocol.md) -- three streams, one command at a time, errors answered.
- [The golden](golden.md) -- the schemas, the reference arithmetic, stimulus and expected outputs.
- [The kernel](kernel.md) -- `PolyKernel.run_iter`, its HLS body, and the generated top.
- [The system flow](system_flow.md) -- the kernel's harness, csynth, the RTL run, the comparison.
- [Timing](timing.md) -- two timed phases, calibrated in the harness and filed under the kernel's
  identity.
- [Behind AXI-MM](memory_mapped.md) -- queue views, a two-thread host, and the composition check.

For the general practice behind the last three -- harnesses, stand-ins, calibrating a unit alone and
composing a parent -- see [Developing module by module](../../guide/flows/units.md).

## Running it

```bash
python -m examples.stream_kernel.stream_kernel_build --through pysim          # pysim only
python -m examples.stream_kernel.stream_kernel_build                          # through compare (Vitis, Vivado)
python -m examples.stream_kernel.stream_kernel_build --through calib_phases   # calibrate the timing
python -m examples.stream_kernel.stream_kernel_build --through mm_compare     # behind AXI-MM
```

The gates are [`tests/examples/test_stream_kernel.py`](../../../tests/examples/test_stream_kernel.py) and
[`tests/examples/test_stream_kernel_mm.py`](../../../tests/examples/test_stream_kernel_mm.py). Cycle
counts on these pages were measured with Vitis HLS and Vivado 2025.1 at 100 MHz.

This kernel is the free-running form of [Streaming polynomial](../stream_inband/index.md), which
teaches the same arithmetic as a host-activated kernel with a status register.
