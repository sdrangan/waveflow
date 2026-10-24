---
title: Legacy examples
parent: Examples
nav_order: 21
has_children: true
summary: "The host-activated examples, kept for the designs and courses that use them: a control-driven kernel the host launches with ap_start, verified by a sequential C++ testbench in Vitis C-simulation and C/RTL co-simulation -- the Legacy flows of the guide. New designs use the system flow and the free-running examples before this group."
---

# Legacy examples

These three examples are the worked material of the **sequential (host-activated) flow**, now under
[Legacy flows](../guide/legacy/index.md) in the guide: one control-driven kernel -- `ap_ctrl_hs` and an
`s_axilite` register map -- that a host launches and waits on, verified by a sequential C++ testbench
([`SeqTB`](../guide/comp_codegen/testbench.md)) in Vitis C-simulation and C/RTL co-simulation. New
designs use the [system flow](../guide/systems/index.md), which the examples before this group teach.

- [Register mapped simple function](regmap/index.md) -- a host-launched kernel over an AXI-Lite register
  map: the launch protocol, a sequential testbench, csim and cosim.
- [Streaming polynomial](stream_inband/index.md) -- the host-launched form of the polynomial, with its
  command-response contract on streams and a status register; its free-running form is
  [Free-running polynomial](stream_kernel/index.md).
- [Histogram with shared memory](shared_mem/index.md) -- a host-launched histogram that reads and writes
  several buffers in memory over one `m_axi` master.
