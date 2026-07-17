---
title: Sequential testbench
parent: Sequential (host-activated)
grand_parent: Realization Flows
nav_order: 5
audience: python
summary: "Defining a SeqTB sequential testbench and what its generated Vitis int main() looks like — the driver that writes inputs, pulses start, and checks outputs, lowered to C++ Vitis runs."
---

# The sequential testbench

<!-- WRITE ME. How to define the SeqTB and what its generated kernel (int main) looks like.
     - The class: SimpFunTBHls(SeqTB) — declares the DUT it drives + a main() body (_is_testbench=True).
     - What it lowers to: a C++ int main() — write inputs to the regmap, pulse start, read + check y.
     - Show the generated main() snippet next to the Python. -->

**Toy example:** `examples/regmap/simp_fun.py` — `SimpFunTBHls`.

**Target:** `sequential_vitis_tb`. `check(SimpFunTBHls, "sequential_vitis_tb")` runs the real
extractor (`extract_testbench`) and validates it.

**Source of truth:** `waveflow/hw/hw_testbench.py` (`SeqTB`, `_is_testbench`, `main`),
`waveflow/build/hwcodegen.py` (`extract_testbench`). See [Comp codegen / testbench](../../comp_codegen/testbench.md).
