---
title: Modules
parent: Guide
nav_order: 5.5
has_children: true
audience: python
summary: "Reference for hardware modules: what a HwModule is (typed ports and a behavior, the single source of truth for a hardware block), its kinds and the cut; writing a free-running module (a leaf's run_iter, a composite's graph, state across firings); developing module by module (harnesses, hostile stand-ins, calibration, composition); the XSI simulation components (kernel, RTL and BFM modules); parameterization; and toward the board. The flow these serve is the Flow Overview."
---

# Modules

A design is a tree of **hardware modules**. The [Flow Overview](../systems/index.md) walks a design
through the flow; these pages are the reference for the modules themselves:

- [Hardware modules](modules.md) -- what a `HwModule` is, its kinds, its hooks, and the cut.
- [Writing a free-running module](concurrent_python.md) -- a leaf's `run_iter`, a composite's graph,
  state across firings.
- [Developing module by module](units.md) -- harnesses and their stand-ins, hostile stand-ins,
  calibrating a module, composing a parent, incremental builds.
- [XSI simulation components](concurrent_layers.md) -- kernel, RTL and BFM modules: what each pysim
  object becomes in an RTL simulation.
- [Parameterization](parametrization.md) -- build parameters, and what a value changes.
- [Toward the board](bitstream_ipi.md) -- a verified system packaged as IP and wired in a block design.

The older host-launched flow is under [Legacy flows](../legacy/index.md).
