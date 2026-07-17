---
title: Free-running component
parent: Concurrent (free-running)
grand_parent: Realization Flows
nav_order: 3
audience: python
summary: "Defining a FreeRunComp — one class that is a leaf (implements run_iter) or a composite (has sub-components), used for both the kernel DUT and the testbench graph. Walked on mem_copy's Sequencer/MemRStream/MemWStream leaves and the MemCopy composite."
---

# The free-running component

<!-- WRITE ME. Defining the free-running component, for BOTH the kernel and the testbench.
     ONE class, two shapes (this is the key idea after the merge — verify against hw_freerun.py):
       - Leaf: implement run_iter (one firing); the base loops it. -> one hls::task.
       - Composite: add_comp children + add_if edges + declare boundary; NO run_iter.
         -> one hls::task per child. A leaf is the 1-task degenerate case of the same walk.
       - _kind() classifies leaf vs composite by content (has a body XOR has children).
     For the DUT: mem_copy's Sequencer (leaf), MemRStream/MemWStream (leaves), MemCopy (composite).
     For the TESTBENCH: MemCopyTB is ALSO a composite FreeRunComp graph (DUT + driver + sink + memory)
       — the same graph runs the pysim golden AND generates the XSI harness (one statement, two backends). -->

**Toy example:** `examples/mem_copy/mem_copy.py` (DUT graph) and `examples/mem_copy/mem_copy_sim.py`
(`MemCopyTB`, the testbench graph).

**Source of truth:** `waveflow/hw/hw_freerun.py` (`FreeRunComp`, `run_iter`, `_kind`, the
derived/overridable `boundary` / `ordered_subcomps` / `internal_edges`). `CompositeComp` is now an
alias for `FreeRunComp` (`waveflow/hw/hw_composite.py`). See [Components / Free-running](../../components/freerun.md)
and [Components / Composite](../../components/composite.md) — note those pages predate the merge and are
being reworked; prefer the code.
