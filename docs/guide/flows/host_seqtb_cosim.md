---
title: Control-driven kernel
parent: Realization Flows
nav_order: 1
audience: python
summary: "Flow 1 — a control-driven kernel (a HostActivated component: ap_ctrl_hs + s_axilite) verified by a sequential Vitis TB (a SeqTB) through C-simulation and RTL co-simulation. The simplest path: the DUT is a function, and the testbench calls it. Full walkthrough coming."
---

# Flow 1 — Control-driven kernel

**DUT output:** a **control-driven kernel** — a [`HostActivated`](../components/hostactivated.md)
component realized as one `ap_ctrl_hs` HLS IP with an `s_axilite` control interface.
**Testbench:** a **sequential Vitis TB** — a [`SeqTB`](../components/) whose `main()` *calls* the kernel,
run under Vitis **C-simulation** (the C++ directly) and **co-simulation** (the generated RTL behind the
same call).

This is the simplest path: because the kernel is a function, the testbench is a straight-line program.
Anchored by `simp_fun` / `poly`.

> **Status: built.** This is the fully-working flow. A step-by-step walkthrough — the `BuildDag` chain
> (codegen → C-sim → C-synth → co-sim), the artifacts each step produces, and how the result is checked
> against the pysim golden — is being written.
