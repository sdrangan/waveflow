---
title: Shared Memory (histogram)
parent: Examples
nav_order: 4
has_children: true
---

# Shared Memory (histogram)

The **shared-memory** pattern moves the *data* off the control plane and into
memory: the accelerator reads its inputs and writes its outputs over an AXI4
memory-mapped (`m_axi`) master, while a dedicated AXI4-Stream still carries the
command and the status response. It is the next step in the
[examples progression](../) after in-band stream control — control stays on a
stream, but the payload now lives in shared memory addressed by pointers in the
command.

The vehicle is a **histogram accelerator** (`examples/shared_mem/`, computation
files keep the `hist` name): given a command carrying three buffer addresses, it
reads `ndata` float samples and `nbins-1` float bin edges from memory, bins the
samples, and writes `nbins` uint32 counts back to memory.

This makes it the first example to exercise, over one `m_axi` bundle:

- **Multiple distinct buffers** at independent addresses (`data_addr`,
  `bin_edges_addr`, `cnt_addr`).
- **Two element types** — `Float32` inputs/edges and `Uint32` counts.
- **Validation → status** — `ndata`/`nbins` bounds checks select a `HistError`
  into the response before any memory access.

Like the other full examples it walks all five stages — Python model → SimPy
simulation → code generation → C and RTL simulation → timing extraction — with
the kernel and testbench generated from the Python `HistAccel` component. It is
the reference design for AXI-MM (`m_axi`) codegen.

## Learning Objectives

In going through this example, you will learn to:

- Move **bulk data off the control plane and into shared DRAM** over an AXI4
  memory-mapped (`m_axi`) master, addressed by pointers carried in the command — while
  control / status stays on a dedicated AXI4-Stream.
- Drive **multiple distinct buffers** at independent addresses (`data_addr`,
  `bin_edges_addr`, `cnt_addr`) and **mixed element types** (`Float32` in, `Uint32`
  out) over **one** `m_axi` bundle.
- **Validate** command bounds and select an error status into the response *before* any
  memory access.
- Use the `MMIFMaster` (`read_array` / `write_array`) interface and the SimPy
  `MemComponent` harness to reach parity with the numpy golden.
- Generate the **multi-buffer `m_axi`** kernel + testbench, run the four-case C-sim,
  C-synth, and RTL cosim, and extract **multi-buffer burst** timing — the reference
  pattern other `m_axi` designs copy.

## Walkthrough

1. [Understanding AXI Memory-Mapped](aximm.md) — what `m_axi` is (burst
   transfers, byte-addressed pointers into shared DRAM), and the shared-memory
   architecture: bulk data in memory, command/response on a dedicated stream.
2. [Python model](python.md) — the `HistAccel` component, the `HistCmd` /
   `HistResp` / `HistError` schemas, and the `MMIFMaster` (`read_array` /
   `write_array`) interface.
3. [Python simulation](pysim.md) — the SimPy harness (`HistController` +
   `MemComponent`) and parity against the numpy golden.
4. [Code generation](codegen.md) — lowering `HistAccel` / `HistTBHls` to the
   multi-buffer m_axi Vitis HLS kernel and testbench.
5. [C and RTL simulation](rtlsim.md) — the four-case C-sim coverage, C-synthesis,
   RTL co-simulation, and multi-buffer burst extraction.
6. [Viewing timing and bursts](timing.md) — the committed timing/burst figures
   and the workflow for refreshing them.
