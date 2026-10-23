---
title: Examples
parent: Waveflow
nav_order: 3
has_children: true
summary: "The reference designs, in reading order, each a complete end-to-end build -- a Python golden model, a simulation, a generated Vitis kernel and testbench, and verification -- and each adding a few concepts to the ones before it. Four groups: the core path through the flow, measuring and exploring, libraries and domains, and the legacy host-activated examples."
---
# Examples

To demonstrate Waveflow, we have developed a set of examples that build progressively
more advanced features. Each one is a complete, end-to-end design — a Python golden
model, a simulation, a generated Vitis HLS kernel and testbench, and verification —
and each introduces a few new concepts on top of the examples before it; its learning objectives
state only that increment. We will add many more over time.

The navigation lists them in reading order, in four groups.

## The flow

The core path, in order: each one is the next rung of the [Flow Overview](../guide/systems/index.md).

1. [Basic vector arithmetic](basic_vec/index.md) -- schemas and fields, vectorized NumPy goldens,
   bit-exact against Vitis: data before any interface.
2. [Free-running polynomial](stream_kernel/index.md) -- one free-running kernel on streams: the
   command-response protocol, the HLS body, the harness, the system flow, calibrated timed phases.
3. [Block equalizer](block_eq/index.md) -- several modules: stages at different rates, a
   `StreamBroadcast`, internal links, each stage in its own harness and then the composite.
4. [The polynomial on a bus](stream_kernel_mm/index.md) -- the same kernel behind memory-mapped
   queues on a crossbar, a two-thread software host on interrupts, timing composed.
5. [Free-running memory copy](memcpy/index.md) -- a kernel that masters memory through
   `MemRStream` / `MemWStream`, whose timing ships pre-fit.
6. [Composite kernel interleaver](interleaver/index.md) -- on-chip random access with a stream of
   blocks, and fitting a custom stage's own timing model.
7. [Block FIR with state](firblock/index.md) -- state across firings (`add_state`), fixed point,
   two kernel bodies for one specification.
8. [A memory reached three ways](bram_access/index.md) -- a memory outside the kernel, as
   hand-written Verilog, shared by two tasks.
9. [A memory-mapped FIR](mm_fir/index.md) -- a register bank, a config ordered against its data
   by an id, and the choice of adaptor topology.
10. [Two kernels on a bus](markov/index.md) -- a kernel as a bus master: a credit stream across the
    crossbar, a shared memory, four bus masters.

## Measuring and exploring

11. [Vector multiply resource modeling](vecmult/index.md) -- a parameter sweep and a resource
    model fitted from it.
12. [What a crossbar costs](xbar_profile/index.md) -- vendor IP measured into a platform's record
    store: a census, not a design.
13. [CG massive-MIMO detector](mimo_cg/index.md) -- the capstone: linear-algebra components
    composed into a bit-exact fixed-point detector.

## Libraries and domains

14. [Waveflow's FFT, at full rate](ssr_fft/index.md) -- a full-rate streaming FFT from free-running
    modules.
15. [Wrapping vendor IP: the Vitis FFT](vitis_fft/index.md) -- a vendor block as a module,
    calibrated as a black box.
16. [RF loopback](rf_loopback/index.md) -- the RF data converter boundary.
17. [Playing a stored waveform](rf_shot_tx/index.md) -- a waveform played out of memory at the
    converter's rate.
18. [Capturing without losing anything](rf_shot_rx/index.md) -- a capture into two memory regions,
    with every loss reported.
19. [Measuring a delay with an address](rf_shot_loopback/index.md) -- the two buffers closed through
    one converter, a delay read as an address difference.
20. [Micro-scheduler on a CPU](cpu_sched/index.md) -- software on a calibrated processor beside
    hardware.
21. [A systolic matrix multiply, measured](systolic/index.md) -- a composite kernel in its own
    harness, measured at RTL against its bounds, and the stage that binds found from a trace.
22. [CG as a vector program](vecunit_cg/index.md) -- an algorithm as three programs for a
    programmable unit, ordered so the unit fuses it, against a dedicated kernel.
23. [A matched filter as a vector program](vecunit_mf/index.md) -- a streaming kernel on the same
    unit, broadcast and conjugate operands, measured against its input floor.

## Legacy

[Legacy examples](legacy.md) -- the host-activated flow (`ap_start`, a sequential testbench, csim and
cosim): [Register mapped simple function](regmap/index.md),
[Streaming polynomial](stream_inband/index.md) and [Histogram with shared memory](shared_mem/index.md).
