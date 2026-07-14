---
title: Register Map (simple function)
parent: Examples
nav_order: 2
has_children: true
---
# Register Map Interface for a Simple Function

This is the first end-to-end example in the interface guide. It walks through the design, simulation,  synthesis, and RTL co-simulation of **standalone control-driven Vitis kernel**.  The kernel uses the simplest of the AXI-* interfaces — an **AXI-Lite register map**.

In going through this example, you will learn to:

- Declare a `VitisRegMap` of typed registers and wire it into a `HostActivated` kernel.
- Write the kernel's behavior as a Python method that reads the input registers, computes a result, and writes it back to the output register.
- Use the kernel's `run_once` function to simulate a **host-activated execution** where a host writes inputs, asserts `ap_start`, polls `status` until done, and reads outputs.
- Create a **sequential testbench** in which a separate host process (`SimpFunHost`) reads test data, calls the `run_once` function and test correctness.
- Generate the corresponding Vitis HLS C++ standalone kernel and Vitis sequential testbench from the Python sources.
- Use appropriate **build steps** to run the Vitis C-simulation and RTL co-simulation flow against the generated artifacts.
- Compare measured RTL cycle timing against the Python model's cycle estimate.
- Visualize the timing on a **VCD** diagram.

## Scalar function example

We illustrate the register map with a simple kernel that computes a clipped affine function:

```python
y = max(0, a * x + b)
```

for signed 32-bit integers `a`, `b`, and `x`. You would not normally build hardware for a function this small, but it isolates exactly one concept — the AXI-Lite register map — without any of the streaming or memory-mapped complexity that a real accelerator brings in.

The kernel has three input registers (`x`, `a`, `b`) and one output register (`y`), plus the standard Vitis control plane that wraps any AXI-Lite kernel. A host driver running on the CPU performs the following sequence to exercise it:

1. Write the three inputs to their register offsets.
2. Write `1` to a specialized register, `ap_start`, to launch the kernel.
3. Poll a  `status` (or wait for an interrupt on `ap_done`) until the kernel signals it is finished.
4. Read `y` from its register offset.

The Python simulation in [`pysim.md`](./pysim.md) implements exactly this sequence in SimPy.

## File map

The Python source, build script, and Vitis driver all live in [`examples/regmap/`](../../../examples/regmap/):

- `simp_fun.py` — the `HostActivated` kernel, its `VitisRegMap`, a SimPy host (`SimpFunHost`), and a `SeqTB` codegen testbench.
- `simp_fun_build.py` — the build DAG: golden Python sim → HLS codegen → Vitis C-sim → C-synth → cosim timing.
- `simp_fun_compute_impl.cpp` — the sticky hand-written body of the kernel's compute hook (the rest of the C++ is generated).
- `timing_diagram.py` — generates a register-trace plot from the co-sim VCD for the synthesis page.
- `run.tcl` — Vitis HLS driver script invoked by the build DAG.

## End-to-end stages

The build DAG in `simp_fun_build.py` chains the standard five-stage pipeline introduced by the [stream_inband example](../stream_inband/):

- **Python golden model** — run the kernel in SimPy and record `y` plus a cycle-accurate timing log.
- **HLS code generation** — emit the Vitis HLS kernel C++ and testbench C++ from the Python source.
- **Vitis C-sim functional verification** — run the generated testbench against the generated kernel and compare its `y` against the Python golden.
- **Vitis C-synth and RTL co-sim timing extraction** — synthesize the kernel, run RTL co-sim, and pull the measured cycle latency out of the cosim report.
- **Timing-diagram generation** — turn the cosim VCD into a register-level trace plot that visually walks through the `ap_start` → `status=busy` → `status=done` handshake.

## Next

- [Understanding Vitis Register Maps](./regmap.md) — a review of register maps and the AXI4-Lite protocol.
- [Python model](./python.md) — declaring the `VitisRegMap` and writing the kernel as a `HostActivated` component.
- [Python Simulation](./pysim.md) — running it in SimPy with the host-side testbench.
- [Vitis HLS Code Generation](./codegen.md) — generating the kernel and testbench C++ from the Python source.
- [C and RTL Simulation](./rtlsim.md) — the Vitis flows and the RTL-vs-Python cycle comparison.
