---
title: Hardware modules and Flows
parent: Guide
nav_order: 5.5
has_children: true
audience: python
summary: "What a hardware module (HwModule) is -- a SimObj with typed ports and a behavior, the single source of truth for a hardware block -- its kinds and the cut, and the flow that takes a module from Python to verified RTL: the system flow, a module in a system (or in its own harness) through csynth, the RTL run under XSI and the comparison with pysim. Writing a free-running module, its flow steps, the XSI simulation components, developing module by module, parameterization, and the path toward the board. The older sequential (host-activated) flow is under Legacy flows."
---

# Hardware modules and Flows

Every Waveflow design starts from a **hardware module** — a [`HwModule`](./modules.md): a `SimObj` with
typed ports and a behavior, the single source of truth for a hardware block. The **[Hardware
modules](./modules.md)** page is the foundation — what a module is, the three things that define it, and
the taxonomy of kinds. This index then covers the **flows**.

A **flow** is the end-to-end recipe for taking a module from its Python specification to a
*verified* hardware realization -- which build steps run, in what order, producing which artifacts, and
how the result is checked. Waveflow has **one**: the system flow.

## The flow

**[Concurrent (free-running)](./concurrent.md)** -- the module runs in a **system**: a DUT and a testbench,
simulated together ([Systems](../systems/index.md)). Free-running modules (`ap_ctrl_none`) are one
`hls::task` for a leaf, one per child for a composite, wired by internal channels; host-activated modules
are devices a host launches over the bus. Verification drives the elaborated RTL cycle by cycle under XSI,
against the same system's pysim. A module is verified first **alone**, in its own
[harness](./units.md), then in the system. The steps are [Building a system](../systems/recipe.md).

The older **sequential flow** -- one host-launched kernel verified by a sequential Vitis testbench and
co-simulation -- is kept under [Legacy flows](../legacy/index.md).

## Why one flow

Only the system flow can express **concurrency**: several tasks running at once, streams between them,
memory shared between them, kernels reached over a bus by a host or by one another -- and RTL
verification that is cycle-accurate across all of it. Most designs worth accelerating need some of
that, and the system flow covers the single kernel too: a kernel in its own harness is a system with no
crossbar.

What follows the flow on the way to a board is not a second flow: [Toward the
board](./bitstream_ipi.md) packages a verified system as IP, one per device, and wires it in a Vivado
block design from the same spec the XSI system top is rendered from. The processor, the board and
the bitstream come next.

## Two targets that are not flows

The targets above are per-**graph**: a DUT plus its testbench. There are two more, and both ask a
per-**module** question:

`xsi_bfm_model` — *can this one module be realized as a pre-written cycle model beside a top?* That is
the realization of a module that lies **outside** the cut, and the peer of `composite_kernel` (inside
it). A module answers it by declaring a `bfm_model()` hook, just as a module inside the cut declares
`kernel_task()`.

[`rtl_module`](../comp_codegen/rtl_module.md) — *can this one module be realized as hand-written
Verilog beside the kernel?* The third member of the same family, for the parts of a design that have
no expression inside a Vitis kernel at all — a memory shared by two concurrent accessors is the
worked case. A module answers it by declaring an `rtl_module()` hook naming a `.v` that already
exists.

Neither is a row in the table, because **the cut is a property of the build, not of the class**: the
same module is inside the DUT in one synthesis, hand-written RTL beside it in another, and a testbench
model in a third, with nothing about the module changed. See
[Hardware modules](./modules.md#the-cut) for that axis, and `check(mod, "<target>")` for the
per-module answer.

## See also

- [Hardware modules](./modules.md) — the module kinds these flows take as input.
- [Build System](../build/) — the `BuildDag` machinery these recipes invoke.
