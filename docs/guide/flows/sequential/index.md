---
title: Sequential (host-activated)
parent: Realization Flows
nav_order: 1
has_children: true
audience: python
summary: "Flow 1 — a control-driven (ap_ctrl_hs + s_axilite) kernel the host launches and waits on, driven by a sequential Vitis testbench Vitis runs in C-simulation and C/RTL co-simulation. The whole flow walked on the simp_fun toy example."
---

# Sequential (host-activated) flow

<!-- WRITE ME. Overview of the sequential, host-activated flow.
     - What it is: a host launches an ap_ctrl_hs kernel over s_axilite, blocks until ap_done.
     - Pros: Vitis co-simulates it directly (start/done handshake); simplest to verify.
     - Cons: one call at a time, host-serialized; no free-running concurrency.
     - When to use it: a kernel invoked per job by host software.
     Toy example for every page in this section: simp_fun (examples/regmap/simp_fun.py). -->

Targets: **`control_driven_kernel`** (DUT) + **`sequential_vitis_tb`** (testbench) — both built
(`waveflow/hw/codegen_targets.py`).

## Pages

1. [Flow steps](./flowsteps.md)
2. [Host-activated component](./hostactivated.md)
3. [Kernel codegen](./codegen.md)
4. [Sequential testbench](./seqtb.md)
5. [C-sim and RTL-sim](./seqsim.md)
6. [Parameterization](./parametrization.md)
