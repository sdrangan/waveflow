---
title: XSI testbench codegen
parent: Concurrent (free-running)
grand_parent: Realization Flows
nav_order: 6
audience: python
summary: "Generating the top-level XSI testbench harness: tb_top_spec walks the CompositeComp testbench graph (DUT boundary + BFM participants wired by interfaces) and render_tb_harness emits the harness header — which models exist, which RTL port each drives, and the fixed-N cycle loop. The testbench .cpp is then just the test."
---

# Generating the XSI testbench

<!-- WRITE ME. How the testbench graph becomes the generated harness.
     - tb_top_spec(MemCopyTB) walks the graph: the DUT is found by its boundary; each boundary port is
       matched to the participant wired to it (found by bfm_model()); a shared memory becomes one
       FlatMemory behind the read/write m_axi slaves.
     - render_tb_harness emits mem_copy_tb_harness.h: the model instances, the sample/update/drive
       dispatch, and the run(n_cycles) loop.
     - render_vectors_h -> the command words (from CopyCmd.serialize()) + scenario; render_ports_h ->
       the RTL port map.
     - What's LEFT hand-written: the ~90-line mem_copy_bfm_tb.cpp is almost entirely the TEST — put a
       known pattern in memory, h.run(N), then check every dst region is a memcpy. Show it.
     - The point: the SAME MemCopyTB graph runs the pysim golden and generates this harness. -->

**Target:** `sequential_xsi_tb`. `check(MemCopyTB, "sequential_xsi_tb")` runs `tb_top_spec` and validates.

**Source of truth:** `waveflow/build/composite_gen.py` (`tb_top_spec`, `render_tb_harness`,
`render_vectors_h`, `render_ports_h`). Generated: `examples/mem_copy/xsi/mem_copy_tb_harness.h`;
the test: `examples/mem_copy/xsi/mem_copy_bfm_tb.cpp`. Cross-reference the memory-wrapper page
([Memory / Streaming Memory Kernels](../../memory/memstream.md)), which walks the same generated harness.
