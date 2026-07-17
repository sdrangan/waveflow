---
title: Concurrent (free-running)
parent: Realization Flows
nav_order: 2
has_children: true
audience: python
summary: "Flow 2 — a free-running (ap_ctrl_none) kernel or composite Vitis cannot co-simulate, verified by driving the elaborated RTL cycle-by-cycle through a concurrent XSI BFM. The whole flow walked on the mem_copy toy example."
---

# Concurrent (free-running) flow

<!-- WRITE ME. Overview of the concurrent, free-running flow.
     - What it is: an ap_ctrl_none kernel with no start/done handshake — one hls::task for a leaf, one
       per child for a composite, wired by internal channels. Runs continuously; a leaf re-fires per job.
     - Pros: true concurrency / pipelining across jobs and across sub-tasks (the reads of job j+1 hide
       behind the writes of job j — see the pipelining note in the memory-wrapper page).
     - Cons: Vitis C/RTL co-sim refuses it (no handshake), so verification is at RTL through XSI.
     - When to use it: a dataflow/streaming accelerator, or any multi-task composite.
     Toy example for every page in this section: mem_copy (examples/mem_copy/). -->

Targets: **`composite_kernel`** (DUT — one target for leaf and composite alike; a leaf is the 1-task
case) + **`sequential_xsi_tb`** (testbench) — both built (`waveflow/hw/codegen_targets.py`).

## Pages

1. [Flow steps](./flowsteps.md)
2. [Free-running component](./freerun.md)
3. [Kernel codegen](./codegen_freerun.md)
4. [Components in XSI](./codegen_xsi.md)
5. [XSI testbench codegen](./codegen_tb.md)
6. [XSI sim](./xsisim.md)
