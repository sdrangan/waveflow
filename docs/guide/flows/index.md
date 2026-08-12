---
title: Hardware modules and Flows
parent: Guide
nav_order: 5.5
has_children: true
audience: python
summary: "What a hardware module (HwModule) is — a SimObj with typed ports and a behavior, the single source of truth for a hardware block — and the two end-to-end recipes for taking one from Python to a verified realization. The flows split on the DUT: a control-driven (ap_ctrl_hs) kernel the host launches and Vitis can co-simulate, driven by a sequential Vitis testbench; or a free-running (ap_ctrl_none) kernel or composite Vitis cannot co-simulate, driven at RTL by a concurrent XSI BFM. The lead page defines the module and its kinds; one sub-section per flow, each walked end to end on a single toy example."
---

# Hardware modules and Flows

Every Waveflow design starts from a **hardware module** — a [`HwModule`](./modules.md): a `SimObj` with
typed ports and a behavior, the single source of truth for a hardware block. The **[Hardware
modules](./modules.md)** page is the foundation — what a module is, the three things that define it, and
the taxonomy of kinds. This index then covers the **flows**.

A **flow** is the end-to-end recipe for taking a module from its Python specification to a
*verified* hardware realization — which build steps run, in what order, producing which artifacts, and
how the result is checked. There are two, and they split on **one axis: the DUT**.

## The two flows

**[Sequential (host-activated)](./sequential.md)** — the DUT is a **control-driven kernel**
(`ap_ctrl_hs` + `s_axilite`) that the host launches and waits on. Because it has a start/done
handshake, Vitis can drive it directly in C-simulation and C/RTL co-simulation, so the testbench is an
ordinary sequential `int main()` (a [`SeqTB`](./sequential.md)). Toy example throughout: `simp_fun`
(`examples/regmap/simp_fun.py`). Targets: `control_driven_kernel` + `sequential_vitis_tb`.

**[Concurrent (free-running)](./concurrent.md)** — the DUT is a **free-running kernel**
(`ap_ctrl_none`): one `hls::task` for a leaf, one per child for a composite, wired by internal
channels. It has no start/done handshake, so Vitis co-sim refuses it; verification instead drives the
elaborated RTL cycle-by-cycle through an **XSI BFM**. Toy example throughout: `mem_copy`
(`examples/mem_copy/`). Targets: `composite_kernel` + `sequential_xsi_tb`.

<!-- One-paragraph pros/cons/when-to-use per flow can go here or in each sub-section's index. -->

A third path — the full system on the fabric (an FPGA `bitstream` via Vivado IPI, no testbench, host
software drives it) — is future work; it is not one of the two simulation flows above.

## One target that is not a flow

The targets above are per-**graph**: a DUT plus its testbench. There is one more, and it asks a
per-**module** question:

`xsi_bfm_model` — *can this one module be realized as a pre-written cycle model beside a top?* That is
the realization of a module that lies **outside** the cut, and the peer of `composite_kernel` (inside
it). A module answers it by declaring a `bfm_model()` hook, just as a module inside the cut declares
`kernel_task()`.

It is deliberately not a row in the table, because **the cut is a property of the build, not of the
class**: the same module is inside the DUT in one synthesis and a testbench model in another, with
nothing about the module changed. See [Hardware modules](./modules.md) for that axis, and
`check(mod, "xsi_bfm_model")` for the per-module answer.

## See also

- [Hardware modules](./modules.md) — the module kinds these flows take as input.
- [Build System](../build/) — the `BuildDag` machinery these recipes invoke.
