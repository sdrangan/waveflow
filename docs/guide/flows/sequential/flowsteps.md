---
title: Flow steps
parent: Sequential (host-activated)
grand_parent: Realization Flows
nav_order: 2
audience: python
summary: "The sequential flow end to end as a step diagram: Python component + SeqTB -> generated ap_ctrl_hs kernel + int main() -> C-sim -> csynth -> C/RTL co-sim."
---

# Flow steps

<!-- WRITE ME. The step-by-step pipeline, with a Mermaid diagram.
     Suggested spine (verify each against the build code before asserting):
       1. Python: HostActivated component (on_start) + SeqTB (main, _is_testbench).
       2. Generate: kernel_files_to_str -> ap_ctrl_hs kernel; the SeqTB -> int main().
       3. C-sim: Vitis runs main() against the C++ model.
       4. csynth: TCL -> RTL.
       5. C/RTL co-sim: the same main() drives the RTL through Vitis's start/done handshake. -->

```mermaid
%% WRITE ME — sketch the pipeline. Placeholder:
flowchart LR
  A[HostActivated + SeqTB<br/>Python] --> B[generate<br/>ap_ctrl_hs kernel + int main]
  B --> C[C-sim]
  B --> D[csynth -> RTL]
  D --> E[C/RTL co-sim]
```

**Source of truth:** `waveflow/build/hwgen.py` (`kernel_files_to_str`), `waveflow/build/hwcodegen.py`
(`extract_kernel` / `extract_testbench`), `waveflow/hw/hw_testbench.py` (`SeqTB`). Targets in
`waveflow/hw/codegen_targets.py`.
