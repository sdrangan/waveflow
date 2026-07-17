---
title: Kernel codegen
parent: Concurrent (free-running)
grand_parent: Realization Flows
nav_order: 4
audience: python
summary: "Generating the free-running synthesizable Vitis kernel: composite_top_spec walks the component/interface graph into an ap_ctrl_none top where each sub-component becomes an hls::task and each internal edge becomes a FIFO. What the emitted C++ looks like, and where the task bodies come from."
---

# Generating the free-running kernel

<!-- WRITE ME. How the graph becomes the ap_ctrl_none top, and what the C++ looks like.
     - composite_top_spec walks the graph -> TopSpec: boundary -> top ports; each ordered_subcomp ->
       one hls::task; each internal_edge -> an hls_thread_local FIFO. render_top emits the .cpp.
     - Each sub-component maps to an hls::task t(task_fn, args...) with its args resolved to top ports
       or internal FIFOs (this is the "sub-components -> hls::task" mapping to describe).
     - #pragma HLS INTERFACE ap_ctrl_none port=return — no start/done handshake.
     - Where the task BODIES come from (the subtle part): TaskBodyStep generates mem_seq_task.h from
       Sequencer.run_iter; MemStreamStep COPIES the hand-written mem_r_stream_task.h /
       mem_w_stream_done_task.h. The dividing line is m_axi, not tops-vs-bodies — a body that owns a
       memory port stays hand-written. (See the memory-wrapper page's "What generates" table.)
     - Show the generated gen/mem_copy.cpp: the tasks + FIFOs + pragmas. -->

**Target:** `composite_kernel`. `check(MemCopy, "composite_kernel")` runs `composite_top_spec` and validates.

**Source of truth:** `waveflow/build/composite_gen.py` (`composite_top_spec`, `render_top`),
`waveflow/build/hwcodegen_steps.py` (`TaskBodyStep`), `waveflow/build/streamutils.py` (`MemStreamStep`).
Generated output: `examples/mem_copy/gen/mem_copy.cpp`. See also
[Concurrency / HLS task](../../concurrency/hls/hlstask.md).
