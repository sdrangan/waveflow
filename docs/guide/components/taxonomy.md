---
title: Component taxonomy
parent: Hardware Components
nav_order: 2
audience: python
applies_to: [HwComponent]
api: [HwComponent, HostActivated, FreeRunComp, CompositeComp]
summary: "The kinds of HwComponent at a glance: a plain HwComponent is a simulation model (like the moving-average filter of the previous page); to make a component synthesizable you pick one of a few subclasses, each with a specialized process shape that maps cleanly to a hardware pattern — HostActivated (host-launched, on_start), FreeRunComp (a continuous loop, run_iter), and CompositeComp (a bodyless hierarchy whose sub-components do the work). One page per kind follows; the synthesis details live in the realization flows."
---

# Component taxonomy

The [previous page](./overview.md) built `MovingAvg` as a plain `HwComponent` — a **simulation model**.
To make a component **synthesizable**, Waveflow provides a few `HwComponent` subclasses, each with a
specialized process shape — a particular way of implementing (or replacing) `run_proc` — that maps
cleanly to a hardware pattern. This page is just the **map** of those kinds; *how* each is built and
verified as hardware is the [realization flows](../flows/) section.

```
HwComponent                  base class — a plain HwComponent is a simulation model (the MovingAvg above)
├── HostActivated            host-launched: you implement on_start; runs once per trigger
├── FreeRunComp              free-running: you implement run_iter; the base loops it forever
└── CompositeComp            structural: no body of its own; its sub-components do the work
```

## Host-activated — `HostActivated`

A component the host launches over a register map: it carries a regmap, and writing `ap_start` runs its
`on_start` once — read the inputs, compute, write the outputs, return. Use it for invocation-style
accelerators. See [Host-activated components](./hostactivated.md).

## Free-running — `FreeRunComp`

A component that runs continuously: you implement `run_iter` — *one firing* — and the base repeats it
forever. Use it for streaming datapaths, like the moving-average filter. See
[Free-running components](./freerun.md).

## Composite — `CompositeComp`

A component with **no body of its own**: it wires sub-components together with internal interfaces, and
they do the work. Use it to build a larger block out of smaller ones. See
[Composite components](./composite.md).
