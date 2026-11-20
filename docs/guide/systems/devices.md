---
title: Devices and the DUT
parent: Waveflow Architecture
nav_order: 0.4
audience: python
api: [MemRStream, MemWStream, MemSlaveAdaptor, MmSlaveDevice, build_mm_device, mm_views, MemoryMod, AXIMMCrossBarIF, DutTop, harness, rtl_module, KernelTask]
summary: "What the DUT is made of: devices, memories and the interconnect. An adaptor is a module with streams on one side and something that is not a stream on the other -- m_axi to memory (MemRStream, MemWStream), an AXI slave (a front and its views), an RF converter edge. A device is kernels plus the adaptors that join them to the world that is not streams; with no adaptor, a device is just its kernels. Kernels and some adaptors (the memory readers and writers, bus writers) have HLS bodies, and together they are the device's HLS top, what Vitis synthesizes; other adaptors (fronts and views) are framework Verilog, and a kernel can be pre-written Verilog too (rtl_module). The device's sub-top joins the HLS top and the RTL into one Verilog module, one IP. The same tree in pysim and at RTL; any module can be the DUT of its own harness. What you write, and what Waveflow provides."
---

# Devices and the DUT

[Kernels](kernels.md) do the work, and talk only on streams. But hardware also reads memory, answers a
bus and raises interrupts. This page is the rest of the DUT: the **adaptors** that join kernels to
everything that is not a stream, the **devices** they form together, and how a device becomes hardware.

## Adaptors

> An **adaptor** is a module with streams on one side and something that is not a stream on the
> other.

| adaptor | its streams | the other side | realized as |
|---|---|---|---|
| a memory reader, [`MemRStream`](../interface/axi_mm/master.md) | a read command in (`MemRCmd`), the data out | `m_axi`: it masters the bus to memory | HLS, provided by Waveflow |
| a memory writer, [`MemWStream`](../interface/axi_mm/master.md) | a write command and the data in (`MemWCmd`), echoed bursts out on `m_done` when the write is done | `m_axi` | HLS, provided by Waveflow |
| a bus writer, for a [credit stream](../interface/derived/credit_stream.md) routed across the bus | the kernel's credit stream | `m_axi`: it writes into a queue on another device | HLS, provided by Waveflow |
| a [slave adaptor](../interface/axi_mm/slave.md): a **front** and its **views** | one per view: queues in and out, a register bank's settings and status | an AXI4 slave a host or another master reaches; an interrupt toward the host | Verilog, provided by Waveflow (Vitis HLS cannot generate an AXI4-full slave) |
| an [RF data converter](../rf/index.md) (`Rfdc`) | sample streams, as AXI-Stream | the ADC or DAC, and the RF world beyond it | AMD's RFDC IP on the board; its emulation in simulation |

The kernel never sees the other side: from the inside, a memory reader and a host's queue look the same --
a stream. The views a kernel needs on the bus are declared on its class, `mm_views`
([views](../interface/axi_mm/slave_views.md)).

## Devices

> A **device** is one or more kernels and the adaptors that join them to the world that is not
> streams.

Device A in the figure below is kernel A behind a slave adaptor: a host reaches it over the bus. Device
C is kernel C between a memory reader and a memory writer: it reads a buffer, computes, and writes the
result. A device with no adaptor is just its kernels: the [polynomial](dut.md) is one kernel on three
streams, and that is the whole DUT.

A device is the unit the hardware is built and packaged in: **one sub-top** in the system's Verilog,
**one IP** when packaged.

![The DUT alone, in a solid box, with a crossbar (purple, vendor IP) on its left reached by the host from outside. Device A, in a dashed box, holds a slave adaptor -- a front with views, green, RTL provided by Waveflow -- and kernel A (blue, a Python model and an HLS body), joined by streams; a dotted box around kernel A alone is device A's HLS top; an interrupt runs from the views out of the DUT to the host. Kernel A sends commands on a stream to kernel C, in device C, a dashed box whose dotted HLS top holds a memory reader, MemRStream, kernel C, and a memory writer, MemWStream (the reader and writer green, provided by Waveflow), the reader and writer on the crossbar through m_axi. A memory, in a dashed box, holds a front with a window and the RAM (green), on the crossbar. A legend: blue, a kernel, yours or a library's; green, an adaptor or memory, provided by Waveflow; purple, vendor IP generated from the address map; dashed, a device or memory, one sub-top and one IP; dotted, the HLS top, what Vitis synthesizes, the rest of the device being RTL.](../flows/figures/dut_tree.svg)

## How a device becomes hardware

Each part of a device is realized one of two ways:

- **HLS.** Every kernel written in Python and HLS, and the adaptors with HLS bodies -- the memory readers
  and writers, the bus writers. Together they are the device's **HLS top**: the one function Vitis
  synthesizes, a task per leaf inside it, its top and csynth script generated
  ([Simulating at RTL](rtl.md#a-modules-hls-body)).
- **Verilog.** The slave adaptor's front and views, and the memories: framework RTL, used as is. A
  module can also be pre-written Verilog of your own, declared with
  [`rtl_module()`](../comp_codegen/rtl_module.md) -- a memory shared by two tasks, which cannot live
  inside an HLS top.

The device's **sub-top** joins the two: the HLS top and the RTL parts, wired into one Verilog module,
derived from the Python, never written ([The system top](../build/xsi_system.md#the-spec)). In device A
the HLS top is kernel A alone and the sub-top adds the slave adaptor; in device C the HLS top is all of
it. With no adaptor, the sub-top is the HLS top.

## The DUT

> The **DUT** is devices, memories, and the interconnect between them.

- **Devices**, as above.
- **Memories** (`MemoryMod`): on-chip or external memory. When a bus reaches it, a memory has its own
  front and window, and is its own sub-top.
- **The interconnect**: streams between devices, and the **crossbar** (`AXIMMCrossBarIF`) when there is
  a bus, joining devices and memories by an address map. It is an interface in Python and AMD's
  `axi_crossbar`, vendor IP, at RTL.

The DUT is one module, or a `DutTop` grouping several with the interfaces between them. It is the same
tree in pysim and at RTL; only the layer around it changes
([Waveflow Architecture](index.md#around-the-dut)). And it is a choice made per run, not a property of a
class: **any module can be the DUT of its own harness**, `harness(Module, workload=...)`, with a
generated stand-in at each stream. Each kernel and each device is simulated alone, at both levels, before
the whole ([The cut](pysim.md#the-cut)).

The code's device class, `MmSlaveDevice` (built by
[`build_mm_device(kernel)`](../interface/axi_mm/slave.md)), covers the bus case: a kernel with its
slave adaptor. A device without a slave adaptor, like device C, is a composite of kernels and memory
adaptors in the code, and its HLS top is its whole sub-top.

## What you write, and what Waveflow provides

| the piece | you write | Waveflow provides |
|---|---|---|
| the messages | the schema classes | the C++ structs, every word packed and unpacked, the array utilities |
| the golden | pure, bit-exact functions | -- |
| a leaf kernel | its **Python model**, `run_iter` (one firing, calling the golden), **and its HLS body**, the same behavior in C++. The two must agree; the RTL comparison checks it | the HLS top around the body, its csynth script; library kernels (stream blocks, the systolic engine, the vector unit, the FFT) |
| a composite kernel | its child kernels and their wiring (`add_comp`, `add_if`) | a task per child, a FIFO per stream between them |
| a device | which adaptors it needs: `mm_views` on the kernel's class; the memory readers and writers as children, and the commands its kernels send them | the adaptors (HLS and Verilog), the address map, the crossbar, the sub-top |
| a memory | its size and where it sits | the memory models and their fronts |
| the testbench | each module's `workload(kind)` and `golden(kind)`; a host's threads, twice: in Python, and in C++ for RTL ([Software threads](../build/sw_threads.md)) | the stand-ins and their C++ twins, the host's generated endpoints, the Verilog top, the build steps |

**The HLS bodies are needed only for the hardware levels**: RTL simulation, and the timing and
resources measured from synthesis ([Timing and resources](calibration.md)). A study that stays in the
golden and pysim needs only Python. A kernel's two copies are written to read the same way
([Defining the kernel in Python and HLS](hwmodule.md)).

Next: [Command-response](command_response.md) -- the protocol kernels speak on their streams.
