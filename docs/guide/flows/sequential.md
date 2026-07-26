---
title: Sequential (host-activated)
parent: Hardware modules and Flows
nav_order: 2
has_children: true
audience: python
summary: "Flow 1 — a control-driven (ap_ctrl_hs + s_axilite) kernel the host launches and waits on, driven by a sequential Vitis testbench Vitis runs in C-simulation and C/RTL co-simulation. The concept here; the full worked walkthrough is the regmap example."
---

# Sequential (host-activated) flow

The sequential flow is the one to reach for first, and the one most kernels in the repo take today. The
DUT is a **control-driven kernel**: a component the host *launches* — it writes the inputs, pulses a
start bit, and waits for the kernel to signal done. In hardware that handshake is `ap_ctrl_hs` over an
`s_axilite` register map; in Python it is a [`HostActivated`](./modules.md) component whose
`on_start` runs once per launch.

Because the kernel starts and finishes, it behaves like a **function** — and that one fact is what
makes this flow the simple one.

## Why it is the simple flow

A function can be *called*. So the testbench is a straight-line program: write the inputs, call the
kernel, check the result. In Vitis **C-simulation** that call is literally a C++ function call —
untimed, with no hardware in sight. And in **co-simulation**, because the DUT is a function, Vitis
generates the RTL test harness for you: it wraps each call as an `ap_start` → wait-for-`ap_done`
transaction against the synthesized RTL. You write a sequential program; you get a cycle-accurate
measurement, without writing a testbench for the hardware.

That is exactly what the [concurrent flow](./concurrent.md) gives up. A free-running kernel never
returns — there is nothing to call and nothing for Vitis to wrap — so it must be driven at RTL by a
hand-built harness. If your design fits the function shape, stay here.

## When to use it, and what it costs

**Use it when** the kernel is invoked per job by host software: one request in, one result out. That
covers most accelerators with a clear request/response boundary.

- **+** Simplest to verify — Vitis co-simulates it directly; there is no RTL testbench to write.
- **+** You get cycle-accurate timing from an ordinary sequential program.
- **−** One call at a time, host-serialized: no free-running concurrency, and no pipelining of
  back-to-back jobs across the kernel boundary.
- **−** The control plane costs an `s_axilite` adapter and a host round-trip per job.

If you need jobs to overlap, that is the [concurrent, free-running flow](./concurrent.md).

## How to read this flow

- The **[flow steps](./sequential_flowsteps.md)** page is the recipe at a glance — the build steps
  from Python to a verified measurement, with a diagram.
- The **[register-map example](../../examples/regmap/)** is the full worked walkthrough on `simp_fun`
  (`y = relu(a·x + b)`): the Python model, the register map, the Python simulation, the generated HLS
  kernel and testbench, and C-sim / RTL co-simulation — every step with its real code.
- **[Parameterization](./parametrization.md)** — how `HwParam` fields become C++ template parameters.

**Targets:** `control_driven_kernel` (the DUT) + `sequential_vitis_tb` (the testbench) — both built.
