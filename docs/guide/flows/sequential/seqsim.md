---
title: C-sim and RTL-sim
parent: Sequential (host-activated)
grand_parent: Realization Flows
nav_order: 6
audience: python
summary: "Running the two Vitis verifications the sequential flow allows: C-simulation (the SeqTB main() against the C++ model) and C/RTL co-simulation (the same main() driving the synthesized RTL through the ap_ctrl_hs handshake)."
---

# C-sim and RTL-sim

<!-- WRITE ME. The two Vitis-run verifications for the sequential flow.
     - C-sim (csim): Vitis compiles + runs the generated int main() against the kernel's C++.
     - csynth: the kernel -> RTL.
     - C/RTL co-sim (cosim): Vitis re-runs the SAME main() driving the RTL, via the start/done
       handshake — which is exactly why this flow needs an ap_ctrl_hs kernel (contrast the concurrent
       flow, where cosim refuses the free-running kernel and XSI is used instead).
     - How to run: the -m vitis tests; the BuildDag steps that invoke Vitis. -->

**Run:** `pytest -m vitis` (csynth / csim / cosim). Needs Vitis HLS installed.

**Source of truth:** `waveflow/toolchain/` (Vitis detection), the `-m vitis` tests under `tests/`, and
the generated `.tcl`. Contrast [Concurrent / XSI sim](../concurrent/xsisim.md) — the free-running flow
cannot use cosim.
