---
title: Memory Copy (composite kernel)
parent: Examples
nav_order: 3
has_children: true
---

# Memory Copy — a free-running composite kernel

This is the worked example for the [concurrent (free-running) flow](../../guide/flows/concurrent.md),
the counterpart to the sequential [register-map example](../regmap/). Where `simp_fun` is a single
host-launched function, `mem_copy` is a **composite of free-running `hls::task`s** that copies a run of
words from one memory region to another — and it is verified not by Vitis co-simulation (which cannot
drive a free-running kernel) but by driving the real RTL cycle-by-cycle through an **XSI BFM**.

The design (`examples/mem_copy/`) is a three-stage pipeline:

`Sequencer` → `MemRStream` → `MemWStream`

a `Sequencer` that turns each copy command into a read command and a write command, a `MemRStream` that
bursts the source region onto a stream, and a `MemWStream` that drains that stream into the
destination. They are wired by internal FIFOs; the whole thing lowers to one `ap_ctrl_none` top with
one `hls::task` per stage.

<!-- WRITE ME — the sub-pages, parallel to the regmap example:
     - python.md    : the composite model (FreeRunComp leaves + the MemCopy composite graph)
     - codegen.md   : composite_top_spec -> the ap_ctrl_none top; TaskBodyStep vs MemStreamStep bodies
     - xsi.md       : what XSI is; tb_top_spec + render_tb_harness -> the generated harness
     - rtlsim.md    : running the XSI sim; the exact cycle count (2835); the pipelining (~177 cyc/job)
     Much of the source material is already in the guide's
     [Streaming Memory Kernels](../../guide/memory/memstream.md) page. -->

## Building blocks

`mem_copy` is built from the framework memory streamers `MemRStream` / `MemWStream`, documented in
[Streaming Memory Kernels](../../guide/memory/memstream.md). That page already walks the generated
composite top and the generated XSI testbench in detail; these pages will build the example up from the
Python model.

**Source:** [`examples/mem_copy/`](../../../examples/mem_copy/).
