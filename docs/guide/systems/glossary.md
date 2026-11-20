---
title: Glossary
parent: Waveflow Architecture
nav_order: 9
audience: python
api: [Simulation, SimObj, HwSystem, DutTop, harness, HwModule, FreeRunMod, HostActivated, KernelTask, MmSlaveDevice, build_mm_device, MemSlaveAdaptor, QueueIn, QueueOut, RegBank, BramWindow, CreditIn, MemRStream, MemWStream, MemoryMod, AXIMMCrossBarIF, Interface, StreamIF, CreditStreamIF, BramIF, IrqIF, SwHost, DataSchema]
summary: "Every term the guide uses for what Waveflow builds and simulates, with its class and the page that defines it: what is simulated (golden, simulation, SimObj, system, DUT, testbench, twin, harness, workload, host), what is built (module, free-running and host-activated modules, leaf, composite, kernel, HLS body, plain module, adaptor, slave adaptor, view, front, device, bus writer, memory reader and writer, memory, crossbar), what joins them and what travels (interface, port, stream, credit stream, BRAM, interrupt, schema, command / response / status), and the words used loosely (module vs kernel, device vs composite, adaptor vs view)."
---

# Glossary

The terms the guide uses for what Waveflow builds and simulates, each with its class and the page that
defines it. How they fit together -- the three levels of simulation, the layer around the DUT, the DUT's
tree -- is [Waveflow Architecture](index.md).

## What is simulated

| term | what it is | class | defined on |
|---|---|---|---|
| **golden** | the operation as pure, bit-exact, untimed functions; every level is checked against it | -- | [The DUT](dut.md#what-it-does-the-golden-first) |
| **simulation** (pysim) | a discrete-event simulation of any collection of `SimObj`s | `Simulation` | [Simulating in Python](pysim.md) |
| **SimObj** | anything that takes part in a simulation; every module is one | `SimObj` | [Simulation](../sim/index.md) |
| **system** | a simulation with one module named the DUT, so it can also run at RTL | `HwSystem(sim, dut)` | [Simulating at RTL](rtl.md#the-system) |
| **DUT** | the design under test: what is synthesized. One module, or a `DutTop` grouping several with the interfaces between them | `DutTop` | [Designing the DUT](dut.md) |
| **testbench** | everything in a system outside the DUT; every part needs an RTL twin | -- | [Simulating at RTL](rtl.md#the-system) |
| **twin** | the C++ model that stands for a testbench part at RTL: a stream driver's `AxisMaster`, a host's C++ program | -- | [BFM testbenches](../build/bfm.md) |
| **harness** | a system generated around one module: a **stand-in** at each port playing the module's workload | `harness(Module, workload=...)` | [The cut](pysim.md#the-cut) |
| **workload** | a module's named scenarios: the bursts its harness plays at each input | `workload(kind)` | [Simulating in Python](pysim.md#the-cut) |
| **host** | software that drives the hardware over the bus: Python threads in pysim, a C++ program at RTL. Testbench, not DUT | `SwHost` | [Software threads](../build/sw_threads.md) |

## What is built

| term | what it is | class | defined on |
|---|---|---|---|
| **module** | typed ports and a behavior: one Python class that is both the model and the source of the hardware | `HwModule` | [Hardware modules](../flows/modules.md) |
| **free-running** | no host starts it: it runs forever, waits on its input streams and acts on each message, one **firing** each. The default kind | `FreeRunMod` | [Kernels](kernels.md#what-a-kernel-is) |
| **host-activated module** | launched by a host over a register map (`ap_start`), runs `on_start` once per launch. Only when the design asks for a launch | `HostActivated` | [Sequential flow](../flows/sequential.md) |
| **leaf** | a kernel with its own body, `run_iter`: one firing, the work on one message; one `hls::task` | `FreeRunMod` | [Kernels](kernels.md#what-a-kernel-is) |
| **composite** | a kernel with child kernels (`add_comp`) wired by streams (`add_if`) and no body; a task per child. A body or children, never both | `FreeRunMod` | [Kernels](kernels.md#what-a-kernel-is) |
| **kernel** | a component of the hardware, described by its operation: a free-running module, a leaf or a composite of kernels. Either can be one Vitis top | `FreeRunMod` | [Kernels](kernels.md) |
| **HLS body** | a kernel's C++, the second copy of its behavior, named by `kernel_task()` | `KernelTask` | [Defining the kernel](hwmodule.md#the-hls-body) |
| **plain module** | a behavioral model Waveflow does not generate (a converter, a channel, a memory model) | `HwModule` | [Hardware modules](../flows/modules.md#the-kinds--and-the-flow-each-maps-to) |
| **adaptor** | what turns a bus, a memory or an interrupt into the streams a module's logic reads and writes | -- | [Stream patterns](streams.md) |
| **slave adaptor** | the adaptor a bus master reaches a kernel through: a front and its views. Framework Verilog at RTL, because Vitis HLS cannot generate an AXI4-full slave | `MemSlaveAdaptor` | [AXI-MM slave](../interface/axi_mm/slave.md) |
| **view** | one way a kernel is reached over the bus, one window of addresses: a queue in or out, a register bank, a BRAM window, a credit queue. Declared on the kernel's class in `mm_views` | `QueueIn`, `QueueOut`, `RegBank`, `BramWindow`, `CreditIn` | [Slave adaptor views](../interface/axi_mm/slave_views.md) |
| **front** | the one part of a slave adaptor that speaks AXI; it passes each beat to the view its address falls in | `axi_slave_front.v` | [AXI-MM slave](../interface/axi_mm/slave.md#the-structure) |
| **device** | a kernel (leaf or composite) with everything that belongs to it on the bus: its views and front, and the bus writers that act for it. Its own sub-top in the Verilog, one IP when packaged | `MmSlaveDevice`, from `build_mm_device(kernel)` | [Simulating at RTL](rtl.md#what-becomes-what) |
| **bus writer** | a module that writes onto the bus for a kernel, brought by a credit stream routed across the bus | -- | [Credit stream](../interface/derived/credit_stream.md) |
| **memory reader / writer** | the master-side adaptors: the logic issues a command on a stream, and they move the data between memory and a stream through `m_axi` | `MemRStream`, `MemWStream` | [AXI-MM master](../interface/axi_mm/master.md) |
| **memory** | DDR or on-chip memory in a system, with its own front and window when a bus reaches it | `MemoryMod` | [Memory Modeling](../memory/index.md) |
| **crossbar** | the AXI bus joining hosts, devices and memories by an address map. An interface in Python; AMD's `axi_crossbar` at RTL | `AXIMMCrossBarIF` | [Simulating at RTL](rtl.md#what-becomes-what) |

## What joins them, and what travels

| term | what it is | class | defined on |
|---|---|---|---|
| **interface** | a typed connection between two modules, or between the DUT and its testbench | `Interface` | [Interfaces](../interface/index.md) |
| **port** (endpoint) | one end of an interface on a module, a master or a slave (`m_out`, `s_in`) | `add_endpoint` | [Defining the kernel](hwmodule.md#the-ports) |
| **stream** | messages in order, each delivered once; **framed** when TLAST ends each message | `StreamIF`, `FramedStreamIFSlave` / `Master` | [Streams](../interface/primitive/stream.md) |
| **credit stream** | a stream whose producer may not overrun a queue across the bus | `CreditStreamIF` | [Credit stream](../interface/derived/credit_stream.md) |
| **BRAM** | a memory shared by address: storage, not an event | `BramIF` | [BRAM](../interface/primitive/bram.md) |
| **interrupt** | "something is ready", from a view to a host | `IrqIF` | [Interrupts](../interface/axi_mm/slave.md#interrupts) |
| **schema** | a message's fields, once, in Python; generated C++ packs every word on both sides | `DataSchema`, `DataList`, `DataArray` | [Data schemas](../schema/index.md) |
| **command, response, status** | the default protocol: a command carrying `tx_id` and `n`, the results headed by a response echoing them, one status per command on its own stream | -- | [Command-response](command_response.md) |

## Words used loosely

- **Module and kernel.** Every kernel is a module; not every module is a kernel. A memory model, a
  channel and a host are modules, not kernels. A composite is a kernel, and so are the memory readers and
  writers Waveflow provides.
- **Device and composite.** A device is about the bus: it exists only when a bus master reaches the
  kernel. A composite is about structure: a kernel made of kernels. A pipeline of kernels on streams,
  or a kernel with memory readers and writers, is a composite; it becomes a device only if a bus
  reaches it too.
- **Adaptor and view.** A slave adaptor holds one or more views behind one front. A memory reader or
  writer is an adaptor too, on the master side.
