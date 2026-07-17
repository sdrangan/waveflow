---
title: Realization Flows
parent: Guide
nav_order: 9.5
has_children: true
audience: python
summary: "The two end-to-end recipes for taking a HwComponent from Python to a verified realization. They split on the DUT: a control-driven (ap_ctrl_hs) kernel the host launches and Vitis can co-simulate, driven by a sequential Vitis testbench; or a free-running (ap_ctrl_none) kernel or composite Vitis cannot co-simulate, driven at RTL by a concurrent XSI BFM. A one-paragraph map here; one sub-section per flow, each walked end to end on a single toy example."
---

# Realization Flows

A **flow** is the end-to-end recipe for taking a component from its Python specification to a
*verified* hardware realization — which build steps run, in what order, producing which artifacts, and
how the result is checked. There are two, and they split on **one axis: the DUT**.

## The two flows

**[Sequential (host-activated)](./sequential/)** — the DUT is a **control-driven kernel**
(`ap_ctrl_hs` + `s_axilite`) that the host launches and waits on. Because it has a start/done
handshake, Vitis can drive it directly in C-simulation and C/RTL co-simulation, so the testbench is an
ordinary sequential `int main()` (a [`SeqTB`](../components/)). Toy example throughout: `simp_fun`
(`examples/regmap/simp_fun.py`). Targets: `control_driven_kernel` + `sequential_vitis_tb`.

**[Concurrent (free-running)](./concurrent/)** — the DUT is a **free-running kernel**
(`ap_ctrl_none`): one `hls::task` for a leaf, one per child for a composite, wired by internal
channels. It has no start/done handshake, so Vitis co-sim refuses it; verification instead drives the
elaborated RTL cycle-by-cycle through an **XSI BFM**. Toy example throughout: `mem_copy`
(`examples/mem_copy/`). Targets: `composite_kernel` + `sequential_xsi_tb`.

<!-- One-paragraph pros/cons/when-to-use per flow can go here or in each sub-section's index. -->

A third path — the full system on the fabric (an FPGA `bitstream` via Vivado IPI, no testbench, host
software drives it) — is future work; it is not one of the two simulation flows above.

## See also

- [Hardware Components](../components/) — the component kinds these flows take as input.
- [Build System](../build/) — the `BuildDag` machinery these recipes invoke.
