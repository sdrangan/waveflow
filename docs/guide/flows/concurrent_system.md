---
title: Systems and the cut
parent: Concurrent (free-running)
grand_parent: Hardware modules and Flows
nav_order: 0.5
audience: python
summary: "The bigger picture of the concurrent flow: a system of several kernels behind a bus, with a memory and a host. Three nested scopes -- the HwSystem (the whole pysim graph), the DUT (the cut: what is synthesized) and each device (a kernel with its views and the bus writers acting for it: one sub-top, one synthesis unit, one IP) -- and how each block is realized: Vitis HLS, hand-written RTL, vendor IP, or a testbench C++ model. The cut is a build choice; SystemTopSpec is the RTL top derived from the DUT. Concepts only, with the details in the build pages."
---

# Systems and the cut

[Writing it in Python](./concurrent_python.md) describes **one kernel**: a leaf with a `run_iter`, or a
composite of tasks. Most designs worth accelerating are larger. They are **systems**: several kernels,
each reached over a bus through a memory-mapped adaptor, an on-chip memory, AMD's crossbar between
them, and a host program that drives it all. This page is the picture of such a system, and the words
the rest of the flow uses for its parts. The [build pages](../build/xsi_system.md) have the details.

## A system, in one picture {#a-system-in-one-picture}

![A generic system. A host (a SwHost, tan) is a bus master on an AXI crossbar (purple, vendor IP). The crossbar's slaves are three sub-tops, each in a dashed box. Device A holds a front with views (green, hand-written RTL), kernel A (blue, Vitis HLS) and a bus writer (blue) that acts for kernel A. Device B holds a front with views and kernel B, which also masters the bus through m_axi. The memory holds a front with a window and the RAM (both green). A solid box around the crossbar and the three sub-tops is the DUT; a dotted box around everything, host included, is the HwSystem. An interrupt line runs from device A's front across the DUT boundary to the host. A legend names the build step that realizes each colour: csynth for blue, system_rtl for green, xbar_ip for purple, system_xsi for tan.](./figures/system_cut.svg)

Each colour is a way of becoming hardware, and the build step that does it:

- **Blue, Vitis HLS.** The kernels, and the bus writers a routed stream brings, are Python modules
  lowered to HLS C++ and synthesized to Verilog by Vitis (the **csynth** step). How one kernel is
  written is [Writing it in Python](./concurrent_python.md).
- **Green, hand-written RTL.** Each memory-mapped front, its views and the memory's RAM are
  framework Verilog, used as is (placed by the **system_rtl** step).
- **Purple, vendor IP.** The crossbar is AMD's `axi_crossbar`, generated from the pysim crossbar's
  address map (the **xbar_ip** step, inside system_rtl).
- **Tan, the testbench.** The host is not synthesized. In pysim it is a model; under XSI it is a C++
  program on a fiber runtime, in the harness the **system_xsi** step generates.

The steps themselves, in order, are on [Flow steps](./concurrent_flowsteps.md#a-systems-steps).

## Three scopes

The boxes in the picture are the three scopes the flow works in:

- **The HwSystem** (dotted) is the whole pysim graph: every module and interface, simulated together by
  `run()`. A system is a class deriving from `HwSystem`, which states what every later step relies on:
  its `sim`, its `run()`, and its `dut()`.
- **The DUT** (solid) is what is synthesized: the devices and the memory, and the crossbar between
  them. `dut()` returns it as a tree, a `DutTop`. Everything outside it is the testbench, which here
  is only the host.
- **A device** (dashed) is one kernel with everything that belongs to it: its views and front, and
  the bus writers that act for it. A routed stream's forward writer carries the sender's data, so it
  sits in the sender's device. Its credit writer returns the receiver's credit, so it sits in the
  receiver's device. A device is the **unit** of everything after simulation: one sub-top of the
  Verilog, one unit to synthesize, one IP to package. The memory is a unit of its own in the same way.

The contract, with markov's class as the example, is
[The system: `HwSystem`](../build/xsi_system.md#the-system).

## The cut is a build choice

DUT and testbench are **roles**, not kinds of module. `dut()` is the cut a build takes when it is given
none, and a build can draw another. Draw it around one device, and the other devices become testbench
models around it. That is how a single device is to be verified on its own; it is planned
(`plans/unit_harness.md`), not built yet. Nothing about a module changes
when the cut moves; only which of its realizations is used. This is the same rule as
[the cut](./modules.md#the-cut) for a single kernel.

## From the DUT to Verilog

The Verilog is derived, never written. The build walks the DUT and produces a **`SystemTopSpec`**:
the RTL top as data, with every instance, crossbar slot, stream net and port. It can be queried
before anything is rendered. The Verilog is rendered from it as one file, with a module per device and
the memory and a system top that instantiates them beside the crossbar.

```text
HwSystem  --dut()-->  DutTop  --walk-->  SystemTopSpec  --render_verilog()-->  <top>.v
```

Because every later step reads that one spec, the simulated top, the synthesized top and each
device's IP cannot disagree. One device can also be rendered alone. That is what a per-device
synthesis or an IP is built from, and what [Systems](../resource/systems.md#per-device) measures.

What the spec holds, how to construct it, and its parameters are in
[The system top: `SystemTopSpec`](../build/xsi_system.md#the-spec).

## Two scales, one idea

The concurrent flow works at two scales:

| scale | the graph | worked example |
|---|---|---|
| **one kernel** | an `HwSystem` whose DUT is the kernel alone, with no crossbar ([XSI simulation components](./concurrent_layers.md)) | [mem_copy](../../examples/memcpy/) |
| **a system** | an `HwSystem` whose `dut()` is a tree of devices | [markov](../../examples/markov/), [mm_fir](../../examples/mm_fir/) |

They are the same idea, a DUT and a testbench drawn by a cut, at two sizes. A single-kernel design is
a system with no crossbar. Its ports leave the top as streams and `m_axi` groups, and testbench models
answer them. mem_copy runs through the system flow that way and reproduces its single-kernel gate
exactly. Most other single-kernel examples still use an older mechanism, a composite testbench
class; moving them over is open work (`plans/system_model.md`).

## Next

- [XSI simulation components](./concurrent_layers.md): the three kinds of module (kernel, RTL, BFM)
  and where each pysim object lands, at either scale.
- [Flow steps](./concurrent_flowsteps.md): the steps for a kernel and for a system.
- [Building the markov system](../../examples/markov/system.md): one system from its modules to its
  DUT tree.
