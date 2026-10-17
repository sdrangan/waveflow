---
title: Legacy flows
parent: Guide
nav_order: 14.5
has_children: true
audience: python
summary: "The sequential (host-activated) flow, kept for the designs and courses that use it: a control-driven kernel the host launches with ap_start, a sequential C++ testbench (SeqTB), Vitis C-simulation and C/RTL co-simulation as the gate. It is the shortest path from a Python function to a verified kernel, and Vitis drives it directly -- but it cannot express concurrency, and Waveflow's system flow is the flow new designs use. Host-activated MODULES are not legacy: an ap_ctrl_hs kernel behind s_axilite is still a kind of device in a system; what is legacy is verifying a design through a sequential testbench."
---

# Legacy flows

Waveflow's flow is the **system flow** ([Systems](../systems/index.md)): a system of concurrent modules,
each verified in its own harness and then together, in pysim and at RTL under XSI. Before it, Waveflow had
a second flow, kept here for the designs and courses that use it.

**The sequential flow** ([Sequential (host-activated)](../flows/sequential.md)) takes one control-driven
kernel -- `ap_ctrl_hs` and an `s_axilite` register map -- that a host launches and waits on, and verifies
it with a sequential C++ testbench ([`SeqTB`](../comp_codegen/testbench.md)) that Vitis runs in
C-simulation and C/RTL co-simulation. It is the shortest path from a Python function to a verified
kernel, and the first one the course teaches. It cannot express concurrency -- several tasks at once,
streams between them, a host on a bus -- which is why new designs use the system flow.

**What is not legacy.** A host-activated **module** -- a kernel the host starts, its registers in a
register map -- is still a kind of module and a kind of device in a system
([Host-activated kernel in HLS](../comp_codegen/hostactivated.md),
[Host launch lifecycle](../comp_codegen/host_launch.md), [Register maps](../interface/axi_mm/regmap.md)).
What is legacy is verifying a design through a sequential testbench instead of a system.

The worked examples of this flow are [Streaming polynomial](../../examples/stream_inband/index.md) (its
free-running successor is [Free-running polynomial](../../examples/stream_kernel/index.md)), and the older
host-activated examples.
