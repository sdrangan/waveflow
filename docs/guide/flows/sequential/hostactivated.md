---
title: Host-activated component
parent: Sequential (host-activated)
grand_parent: Realization Flows
nav_order: 3
audience: python
summary: "Defining a HostActivated component (on_start over a regmap boundary) and running its Python simulation, on simp_fun."
---

# Defining a host-activated component

<!-- WRITE ME. How to define the HostActivated component and run a Python sim.
     - The class: SimpFunComponent(HostActivated), a VitisRegMap boundary (x in, y out) + s_axilite.
     - The body: on_start() reads the regmap, calls a @synthesizable hook, writes the result back.
     - control_mode = PER_INVOCATION (runs once per trigger).
     - Running the pysim: the host (SimpFunHost) writes x, pulses start, reads y. Show the golden. -->

**Toy example:** `examples/regmap/simp_fun.py` — `SimpFunComponent`.

**Source of truth:** `waveflow/hw/hw_hostactivated.py` (`HostActivated`, `on_start`),
`waveflow/hw/regmap.py` (`VitisRegMap`, `VitisRegMapMMIFSlave`). See also
[Components / Host-activated](../../components/hostactivated.md) and [Interface / regmap](../../interface/regmap.md).
