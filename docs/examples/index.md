---
title: Examples
parent: Waveflow
nav_order: 3
has_children: true
---
# Examples

To demonstrate Waveflow, we have developed a set of examples that build progressively
more advanced features. Each one is a complete, end-to-end design — a Python golden
model, a simulation, a generated Vitis HLS kernel and testbench, and RTL verification —
and each introduces one or two new concepts on top of the previous example. We will add
many more over time.

- **[basic_vec](./basic_vec/)** — Perform vectorized operations over integer,
  fixed-point, and floating-point data in Python, and generate **bit-exact** matching
  code in Vitis.
- **[regmap](./regmap/)** — Build a simple **host-activated kernel** on scalar data with
  a **Vitis register map**.
- **[stream_inband](./stream_inband/)** — Construct a simple polynomial kernel with an
  **AXI4-Stream interface** for vector data, with control and data sent **in-band**.
  Represent the control and data via Waveflow **DataSchema** classes.
- **[shared_mem](./shared_mem/)** — Build a simple histogram kernel where data is passed
  via **shared memory** over an **AXI4 memory-mapped** interface.
- **[memcpy](./memcpy/)** — Build a simple **free-running** kernel for copying data in
  memory.
- **[vecmult](./vecmult/)** — Measure and model what a design **costs**: sweep a free-running
  vector multiplier across its parameters, and turn the measurements into **resource models**
  — formulas where the hardware is derivable, a fit where it is not.
- **[interleaver](./interleaver/)** — Build a simple interleaver over memory locations
  using a **load–compute–store** dataflow pipeline.
