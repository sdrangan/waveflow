---
title: Timing Analysis Tools
parent: Guide
nav_order: 11
has_children: true
---

# Waveflow Timing Analysis Tools

Waveflow provides some basic package for python processing of timing diagrams relevant for hardware workflows:

- Creating timing and plotting timing diagrams in matplotlib
- Running Xilinx RTL simulations to produce VCD outputs
- Extracting information from timing diagrams
- Python-based analysis for AXI protocols including AXI-Lite, AXI-Streaming, and AXI memory-mapped

For a free-running (`ap_ctrl_none`) `hls::task` kernel, [Tracing a kernel run](./trace_steps.md)
covers the four build steps that dump a VCD of its internal channels and bind the signals by exact
name; [Trace pitfalls](./trace_pitfalls.md) collects the three subtle ways such a measurement goes
silently wrong (sampling phase, `ap_done` anchoring, occupancy vs write-enable). The
[memcpy timing](../../examples/memcpy/timing.md) example applies the whole flow to one kernel.

