---
title: Waveflow Architecture
parent: Guide
nav_order: 1.2
has_children: true
audience: python
api: [Simulation, SimObj, HwSystem, DutTop, harness, HwModule, FreeRunMod, HostActivated, MmSlaveDevice, build_mm_device, MemSlaveAdaptor, MemRStream, MemWStream, MemoryMod, AXIMMCrossBarIF, StreamIF, CreditStreamIF, BramIF, IrqIF, SwHost, DataSchema]
summary: "What Waveflow builds and simulates, before how -- where the guide starts. A design is a concurrent system, and Waveflow runs it at three levels: the golden (untimed, bit-exact pure functions, for accuracy and design-space search), pysim (a Simulation of SimObjs, one of them the DUT, with timing), and RTL simulation (an HwSystem: the DUT as Verilog, its testbench as C++ twins). The DUT is the same tree at both simulated levels -- devices (a kernel with its slave adaptor: a front and views), composites, kernels (a free-running leaf: run_iter in Python, an HLS body at RTL), memory adaptors, the crossbar -- and only the layer around it changes. What you write (schemas, the golden, run_iter, the HLS body, the wiring, mm_views, workloads, a host) and what Waveflow provides. Any module can be the DUT of its own harness. A glossary of every term, with its class and page. Then the Flow Overview: how it is built, step by step."
---

# Waveflow Architecture

**Waveflow models concurrent systems.** A design is several things running at once -- kernels,
memories, a host program, the world around them -- joined by streams and buses, and the questions worth
asking are about the whole: does it compute the right answer, how fast, at what cost. Waveflow
describes the hardware once, in Python, and runs that one description at three levels.

This page is **what** is built and simulated. How to build it, step by step, is the
[Flow Overview](flow.md).

## Three levels of simulation {#three-levels}

| level | what runs | it answers | used for |
|---|---|---|---|
| **golden** | the operation as pure, bit-exact functions: no time, no modules | does it compute the right answer | accuracy studies; design-space search at scale; the reference every other level is checked against |
| **pysim** | the DUT's modules in a [`Simulation`](pysim.md), with timing, beside anything else the question needs | how fast, and does it work as part of the system | exploring and evaluating the design; each module alone, then together |
| **RTL simulation** | the DUT's Verilog in an [`HwSystem`](rtl.md#the-system), cycle by cycle, its testbench as C++ twins | does the hardware do what the Python does | verifying the hardware; measuring it |

The levels are not three descriptions of the design. The **golden** is a set of functions; a
kernel's Python behavior (`run_iter`) **calls** them, so pysim computes exactly what the golden does and
adds time. RTL runs the hardware written to read the same way, and is compared with pysim word for
word. In the running example, the golden's `poly_eval` is what
[`PolyKernel.run_iter`](../../../examples/stream_kernel/stream_kernel.py) evaluates.

The golden takes three forms, all from the same functions: the **functions** themselves (what it
does, [The DUT](dut.md#what-it-does-the-golden-first)); a module's **`golden(kind)`**, the bursts each
output must record for a workload, which its harness checks pysim against
([verification](verification.md)); and a design's **`golden(params, workload)`** for design-space
search, the accuracy metrics of an untimed run ([DSE context](../dse/context.md)).

## The pysim hierarchy

```
Simulation                     a discrete-event simulation of SimObjs
├── SimObj ...                 anything the question needs: a host program, a wireless channel,
│                              a traffic source, a processor model
└── DUT                        a module (a module is a SimObj too): the tree below
```

**Anything can join a `Simulation`.** The DUT is the part that will become hardware; everything
around it is the world, at whatever level of detail the question needs
([Simulating in Python](pysim.md)).

## The RTL hierarchy

```
HwSystem(sim, dut)             a Simulation with one module named the DUT; flat
├── testbench                  everything outside the DUT, derived, never listed:
│                              hosts, stream drivers and sinks, memory models -- each with a C++ twin
└── DUT                        what is synthesized: the same tree as in pysim
```

**Only what has an RTL twin can join an `HwSystem`.** A host has a C++ twin, so it can drive the RTL;
a wireless channel model has none, so a study with one is a pysim study, and its RTL simulation draws
the testbench where everything has a twin ([Simulating at RTL](rtl.md)).

![A generic system. A host (a SwHost, tan) is a bus master on an AXI crossbar (purple, vendor IP). The crossbar's slaves are three sub-tops, each in a dashed box. Device A holds a front with views (green, hand-written RTL), kernel A (blue, Vitis HLS) and a bus writer (blue) that acts for kernel A. Device B holds a front with views and kernel B, which also masters the bus through m_axi. The memory holds a front with a window and the RAM (both green). A solid box around the crossbar and the three sub-tops is the DUT; a dotted box around everything, host included, is the HwSystem. An interrupt line runs from device A's front across the DUT boundary to the host. A legend names the build step that realizes each colour: csynth for blue, system_rtl for green, xbar_ip for purple, system_xsi for tan.](../flows/figures/system_cut.svg)

## Inside the DUT: the same tree at both levels {#the-dut-tree}

```
DUT                            one module, or a DutTop grouping several
├── device                     a kernel or composite + its slave adaptor; one IP, reached over a bus
│   ├── slave adaptor          a front (the one part that speaks AXI) + views (queues, a register bank,
│   │                          a BRAM window): the bus, as streams
│   └── kernel or composite    the logic, streams only
├── composite                  children wired by internal interfaces; no body; the children run concurrently
│   ├── kernel                 a free-running leaf: one run_iter -> one hls::task
│   └── MemRStream / MemWStream  memory, as streams
├── crossbar                   the AXI bus between devices and memories
└── memory
```

Under every part sits one rule: **a module's own logic reads and writes only streams.** A bus, a memory
and an interrupt reach it through **adaptors** -- a slave adaptor's views on the bus side, memory readers
and writers on the master side -- outside its logic ([Stream patterns](streams.md)). The **interfaces**
between modules are typed connections (streams, credit streams, BRAM, interrupts), and the **messages**
on them are [schemas](../schema/index.md).

The DUT is a choice made per run, not a property of a class: **any module can be the DUT of its own
harness**, `harness(Module, workload=...)`, with a generated stand-in at each port. Each module is
simulated alone, at both levels, before the whole ([The cut](pysim.md#the-cut)).

## What you write, and what Waveflow provides {#what-you-write}

| the piece | you write | Waveflow provides |
|---|---|---|
| the messages | the schema classes | the C++ structs, every word packed and unpacked, the array utilities |
| the golden | pure, bit-exact functions | -- |
| a kernel | its **Python model**, `run_iter` (one firing, calling the golden), **and its HLS body**, the same behavior in C++. The two must agree; the RTL comparison checks it | the HLS top around the body, its csynth script |
| a composite | its children and their wiring (`add_comp`, `add_if`) | a task per child, a FIFO per internal interface |
| a device | the views on the kernel's class (`mm_views`) | the views and the front (framework RTL), the address map, the crossbar |
| memory access | the commands the logic issues | `MemRStream` / `MemWStream`, the memory models |
| the testbench | each module's `workload(kind)` and `golden(kind)`; a host's threads | the stand-ins, the C++ twins, the Verilog top, the build steps |

**The HLS body is needed only for the hardware levels**: RTL simulation, and the timing and resources
measured from synthesis ([Timing and resources](calibration.md)). A study that stays in the golden and
pysim needs only Python. A kernel's two copies are written to read the same way
([Defining the kernel in Python and HLS](hwmodule.md)).

## Glossary {#glossary}

### What is simulated

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

### What is built

| term | what it is | class | defined on |
|---|---|---|---|
| **module** | typed ports and a behavior: one Python class that is both the model and the source of the hardware | `HwModule` | [Hardware modules](../flows/modules.md) |
| **free-running module** | runs forever on its streams, firing once per message. The default kind | `FreeRunMod` | [Hardware modules](../flows/modules.md#the-kinds--and-the-flow-each-maps-to) |
| **host-activated module** | launched by a host over a register map (`ap_start`), runs `on_start` once per launch. Only when the design asks for a launch | `HostActivated` | [Sequential flow](../flows/sequential.md) |
| **leaf** | a module with a body, `run_iter`: one firing, the work on one message | `FreeRunMod` | [Designing the DUT](dut.md#its-modules) |
| **composite** | a module with children (`add_comp`) wired by internal interfaces (`add_if`) and no body. A body or children, never both | `FreeRunMod` | [Designing the DUT](dut.md#its-modules) |
| **kernel** | a free-running leaf as hardware: its `run_iter` and its HLS body, one `hls::task` | `FreeRunMod` | [Defining the kernel](hwmodule.md) |
| **HLS body** | a kernel's C++, the second copy of its behavior, named by `kernel_task()` | `KernelTask` | [Defining the kernel](hwmodule.md#the-hls-body) |
| **plain module** | a behavioral model Waveflow does not generate (a converter, a channel, a memory model) | `HwModule` | [Hardware modules](../flows/modules.md#the-kinds--and-the-flow-each-maps-to) |
| **adaptor** | what turns a bus, a memory or an interrupt into the streams a module's logic reads and writes | -- | [Stream patterns](streams.md) |
| **slave adaptor** | the adaptor a bus master reaches a kernel through: a front and its views. Framework Verilog at RTL, because Vitis HLS cannot generate an AXI4-full slave | `MemSlaveAdaptor` | [AXI-MM slave](../interface/axi_mm/slave.md) |
| **view** | one way a kernel is reached over the bus, one window of addresses: a queue in or out, a register bank, a BRAM window, a credit queue. Declared on the kernel's class in `mm_views` | `QueueIn`, `QueueOut`, `RegBank`, `BramWindow`, `CreditIn` | [Slave adaptor views](../interface/axi_mm/slave_views.md) |
| **front** | the one part of a slave adaptor that speaks AXI; it passes each beat to the view its address falls in | `axi_slave_front.v` | [AXI-MM slave](../interface/axi_mm/slave.md#the-structure) |
| **device** | a kernel or composite with everything that belongs to it on the bus: its views and front, and the bus writers that act for it. Its own sub-top in the Verilog, one IP when packaged | `MmSlaveDevice`, from `build_mm_device(kernel)` | [Simulating at RTL](rtl.md#what-becomes-what) |
| **bus writer** | a module that writes onto the bus for a kernel, brought by a credit stream routed across the bus | -- | [Credit stream](../interface/derived/credit_stream.md) |
| **memory reader / writer** | the master-side adaptors: the logic issues a command on a stream, and they move the data between memory and a stream through `m_axi` | `MemRStream`, `MemWStream` | [AXI-MM master](../interface/axi_mm/master.md) |
| **memory** | DDR or on-chip memory in a system, with its own front and window when a bus reaches it | `MemoryMod` | [Memory Modeling](../memory/index.md) |
| **crossbar** | the AXI bus joining hosts, devices and memories by an address map. An interface in Python; AMD's `axi_crossbar` at RTL | `AXIMMCrossBarIF` | [Simulating at RTL](rtl.md#what-becomes-what) |

### What joins them, and what travels

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

### Words used loosely

- **Module and kernel.** Every kernel is a module; not every module is a kernel. A composite, a memory
  reader, a memory model and a host are modules, not kernels.
- **Device and composite.** A device is about the bus: it exists only when a bus master reaches the
  module. A pipeline of kernels on streams, or a kernel with memory readers and writers, is a composite;
  it becomes a device only if a bus reaches it too.
- **Adaptor and view.** A slave adaptor holds one or more views behind one front. A memory reader or
  writer is an adaptor too, on the master side.

Next: [Flow Overview](flow.md) -- how a design is built and checked, step by step.
