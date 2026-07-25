---
title: Register Map (simple function)
parent: Examples
nav_order: 2
has_children: true
---
# Register Map Interface for a Simple Function

This is the first end-to-end example in the interface guide. It walks through the design, simulation,
synthesis, and RTL co-simulation of a **standalone control-driven Vitis kernel**. The kernel uses the
simplest of the AXI-* interfaces — an **AXI-Lite register map**.

## Learning Objectives

In going through this example, you will learn to:

- Declare a `VitisRegMap` of typed registers and wire it into a `HostActivated` kernel.
- Write the kernel's behavior as a Python method that reads the input registers, computes a result, and writes it back to the output register.
- Model a **host** as a `SimObj` that drives the register-map protocol — write the inputs, assert `ap_start`, poll `ap_done`, read the result.
- Run a **system simulation** in Python and *confirm the model works* before writing any testbench.
- Write a **`SeqTB`** — a sequential testbench that invokes the kernel and times it.
- Generate the Vitis HLS C++ kernel and testbench from those same Python sources.
- Run the Vitis C-simulation and RTL co-simulation against the generated artifacts.
- Compare the measured RTL cycle timing against the Python model's estimate, and visualize the handshake on a **VCD** diagram.

## Scalar function example

We illustrate the register map with a simple kernel that computes a clipped affine function:

```python
y = max(0, a * x + b)
```

for signed 32-bit integers `a`, `b`, and `x`. You would not normally build hardware for a function this
small, but it isolates exactly one concept — the AXI-Lite register map — without any of the streaming or
memory-mapped complexity that a real accelerator brings in.

The kernel has three input registers (`x`, `a`, `b`) and one output register (`y`), plus the standard
Vitis control plane that wraps any AXI-Lite kernel. A host driver running on the CPU performs the
following sequence to exercise it:

1. Write the three inputs to their register offsets.
2. Write `1` to a specialized register, `ap_start`, to launch the kernel.
3. Poll `ap_done` (or wait for an interrupt) until the kernel signals it is finished.
4. Read `y` from its register offset.

## Two ways to simulate it

The same kernel is exercised **two** different ways, and the difference between them is the thread that
runs through this whole example:

|               | **System simulation**                                                                            | **Sequential execution**                                         |
| ------------- | ------------------------------------------------------------------------------------------------------ | ---------------------------------------------------------------------- |
| What runs     | a host`SimObj` running **concurrently** with the kernel, exchanging real AXI-Lite transactions | **one** sequential program (a `SeqTB`) that invokes the kernel |
| What you see  | the**protocol** — `ap_start` → poll → `ap_done` — plus a per-step event trace            | the functional result and the total transaction latency                |
| Where it runs | **Python only**                                                                                  | **Python, C-simulation, and RTL co-simulation**                  |
| Page          | [System simulation](./pysim.md)                                                                         | [Sequential execution](./seqtb.md)                                      |

That "Python only" is not an omission — it is **fundamental**. A system simulation is *concurrent*: the
host and the kernel are two independent processes talking over a link. A Vitis C++ testbench is a single
straight-line `int main()`, so there is nothing for that concurrency to lower onto. The sequential path,
by contrast, is one program calling a function — which is exactly what a C++ testbench *is*, so the very
same `SeqTB.main()` runs in Python, in C-simulation, and in co-simulation.

Both paths are driven by the **same** `x`/`a`/`b` vector, so they independently produce the same `y` —
the concurrent host-protocol model and the sequential direct-invocation model agreeing.

> Simulating a *concurrent* design down at RTL is the [concurrent flow](../../guide/flows/concurrent.md):
> a free-running kernel Vitis cannot co-simulate, driven at RTL through an XSI BFM. That is the
> [mem_copy example](../memcpy/); this page stays with the sequential path Vitis co-simulates directly.

## The progression

The build follows the order you would actually write this design in, and each step produces an artifact
you can confirm before moving on:

1. **Write the kernel** — the `HostActivated` DUT and its `VitisRegMap` → [Python model](./python.md)
2. **Write the host** — a `SimObj` that drives the register protocol (no C++ is ever generated from it) → [Python model](./python.md)
3. **Run the system simulation** — confirm the model works end-to-end, in Python → [System simulation](./pysim.md)
4. **Write the `SeqTB`** — the sequential testbench → [Sequential execution](./seqtb.md)
5. **Generate and run the C++** — the same testbench in C-simulation, then RTL co-simulation → [Code generation](./codegen.md), [C and RTL simulation](./rtlsim.md)
6. **Inspect the timing** — compare the Python estimate against the measured RTL cycles → [C and RTL simulation](./rtlsim.md)

## File map

The Python source, build script, and Vitis driver all live in [`examples/regmap/`](../../../examples/regmap/):

- `simp_fun.py` — the `HostActivated` kernel and its `VitisRegMap`, the `SimpFunHost` `SimObj`, and the `SimpFunTBHls` `SeqTB`.
- `simp_fun_build.py` — the build DAG (below).
- `simp_fun_compute_impl.cpp` — the sticky hand-written body of the kernel's compute hook (the rest of the C++ is generated).
- `timing_diagram.py` — generates a register-trace plot from the co-sim VCD.
- `run.tcl` — Vitis HLS driver script invoked by the build DAG.

## End-to-end stages

The build DAG in `simp_fun_build.py` runs the progression above as typed steps, each declaring the
artifacts it consumes and produces:

- **`build_inputs`** — write the `x`/`a`/`b` test vector to `.bin` files. Every stage below is driven by these same numbers.
- **`system_sim`** — the Python-only system simulation: host + kernel, concurrently. Writes a pass/fail verdict and the protocol event trace, and **fails the build** if the run does not verify.
- **`py_sim`** — run the `SeqTB` in Python: the functional golden the C-simulation is checked against, plus the measured transaction latency (`py_timing`).
- **`gen_kernel` / `gen_tb`** — emit the Vitis HLS kernel C++ and the testbench C++ from the same Python sources.
- **`csim` → `validate_csim`** — run the generated testbench against the generated kernel and compare its `y` against the Python golden.
- **`csynth` → `cosim` → `extract_cosim_timing` → `validate_timing`** — synthesize, run RTL co-simulation, pull out the measured cycle latency, and check it against the Python estimate.
- **`generate_timing_diagram`** — turn the co-sim VCD into a register-level trace plot of the `ap_start` → busy → `ap_done` handshake.

Run any prefix of it with `--through <step>`, e.g. `python simp_fun_build.py --through system_sim`.

## Next

- [Understanding Vitis Register Maps](./regmap.md) — a review of register maps and the AXI4-Lite protocol.
- [Python model](./python.md) — the `VitisRegMap`, the `HostActivated` kernel, and the host.
- [System simulation](./pysim.md) — the Python-only host + kernel simulation, and confirming it.
- [Sequential execution](./seqtb.md) — the `SeqTB`, and the path to C-sim and co-sim.
- [Vitis HLS Code Generation](./codegen.md) — generating the kernel and testbench C++ from the Python source.
- [C and RTL Simulation](./rtlsim.md) — the Vitis flows and the RTL-vs-Python cycle comparison.
