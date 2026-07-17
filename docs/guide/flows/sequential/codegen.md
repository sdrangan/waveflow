---
title: Kernel codegen
parent: Sequential (host-activated)
grand_parent: Realization Flows
nav_order: 4
audience: python
summary: "Generating the Vitis kernel from a HostActivated component, and what the emitted C++ looks like: an ap_ctrl_hs top with an s_axilite control interface, extracted from on_start."
---

# Generating the kernel

<!-- WRITE ME. How the HostActivated component becomes a Vitis kernel, and what the C++ looks like.
     - The generator: kernel_files_to_str(SimpFunComponent) -> the .hpp/.cpp (+ hook impl stubs).
     - What it emits: an ap_ctrl_hs top; s_axilite maps the regmap fields; on_start is the body.
     - The @synthesizable hook: emitted as a hand-written impl (TODO stub if absent), not lowered.
     - Show a snippet of the generated .cpp: the pragmas + the on_start-derived body. -->

**Target:** `control_driven_kernel`. `check(SimpFunComponent, "control_driven_kernel")` runs the real
extractor and validates it.

**Source of truth:** `waveflow/build/hwgen.py` (`kernel_files_to_str`), `waveflow/build/hwcodegen.py`
(`extract_kernel`), `waveflow/build/codegen_dispatch.py` (dispatch on the kind). See
[Comp codegen](../../comp_codegen/) for the extractor detail.
