---
title: Kernel codegen
parent: Memory Copy
nav_order: 5
---

# Kernel codegen

<!-- WRITE ME — how the MemCopy graph becomes the ap_ctrl_none top:
     - composite_top_spec walks the graph -> render_top emits the top (one hls::task per child, one
       internal FIFO per edge, boundary ports from the boundary list).
     - Task bodies: TaskBodyStep generates mem_seq_task.h from Sequencer.run_iter; MemStreamStep copies
       the hand-written mem_r/w bodies (they own m_axi). The dividing line is m_axi, not tops-vs-bodies.
     - Show gen/mem_copy.cpp: the tasks + FIFOs + #pragma HLS INTERFACE ap_ctrl_none.
     Source material: guide/memory/memstream.md "What generates" table. -->
