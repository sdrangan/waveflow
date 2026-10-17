---
title: Systems
parent: Guide
nav_order: 1.2
has_children: true
audience: python
summary: "Waveflow models concurrent systems, and this section is where the guide starts. A system is flat: a testbench and a DUT, simulated together in pysim and at RTL. The DUT is a tree of modules -- kernels, memories, the devices that put kernels on a bus -- wired by interfaces that carry schemas. The cut is a build choice: any module in the tree can be made the DUT of its own system, its harness, so a design is developed module by module and composed. The figure walked top-down, with a link from each part to the section that defines it; a ladder of worked examples from one kernel on streams to several kernels, a memory and a host on a bus; and the recipe for building one."
---

# Systems

**Waveflow models concurrent systems.** A design is several things running at once -- kernels, memories,
a host program -- joined by streams and buses, and the questions worth asking are about the whole: does
it compute the right answer, how fast, at what cost. Waveflow describes such a system once, in Python,
and runs that one description in a fast Python simulation (pysim) and, cycle by cycle, as RTL.

This section is the top of the guide: what a system is, its parts from the outside in, and how to build
one. Each part links to the section that defines it.

## A system, in one picture

![A generic system. A host (a SwHost, tan) is a bus master on an AXI crossbar (purple, vendor IP). The crossbar's slaves are three sub-tops, each in a dashed box. Device A holds a front with views (green, hand-written RTL), kernel A (blue, Vitis HLS) and a bus writer (blue) that acts for kernel A. Device B holds a front with views and kernel B, which also masters the bus through m_axi. The memory holds a front with a window and the RAM (both green). A solid box around the crossbar and the three sub-tops is the DUT; a dotted box around everything, host included, is the HwSystem. An interrupt line runs from device A's front across the DUT boundary to the host. A legend names the build step that realizes each colour: csynth for blue, system_rtl for green, xbar_ip for purple, system_xsi for tan.](../flows/figures/system_cut.svg)

Read it from the outside in:

1. **The system** (dotted) is everything simulated together: an [`HwSystem`](../build/xsi_system.md#the-system),
   a simulation plus **one named DUT**. It is flat -- a testbench and a DUT, nothing nested.
2. **The testbench** is everything outside the DUT, derived, never listed: here the host, a
   [`SwHost`](../build/sw_threads.md) whose threads are Python in pysim and a C++ twin at RTL. In a
   smaller system the testbench is stream drivers and sinks.
3. **The DUT** (solid) is what is synthesized. It is a **tree of modules**: here three devices and the
   crossbar between them.
4. **A device** (dashed) is a kernel with everything that belongs to it -- its memory-mapped views and
   front, the bus writers that act for it -- packaged as one IP. A device is a composite module like any
   other; being packaged as one is a fact about it, not a kind.
5. **A module** is a [`HwModule`](../flows/modules.md): typed ports and a behavior. A **kernel** is a
   free-running module whose behavior is a Python `run_iter` and, at RTL, a hand-written or generated HLS
   body ([Modules](../flows/modules.md)); a host-activated kernel is the other kind.
6. **The interfaces** between modules are typed connections -- streams, credit streams, AXI-MM, BRAM,
   interrupts ([Interfaces](../interface/index.md)).
7. **The messages** on them are [schemas](../schema/index.md): the fields once, in Python, and every
   word packed by generated code.

The colours are the ways a part becomes hardware: **blue** is Vitis HLS (the kernels, by csynth),
**green** is hand-written framework RTL (the views and the memory), **purple** is vendor IP (the
crossbar), **tan** is the testbench (never synthesized).

## The cut is a build choice

DUT and testbench are **roles**, not kinds of module. Draw the cut around any module in the tree and
the rest becomes the testbench: [`harness(module)`](../flows/units.md) makes a module the DUT of its own
system, with a stand-in generated at each of its ports. That is how a system is **developed module by
module**: each kernel verified, and its timing calibrated, alone; then composed, with nothing fitted to
the whole, and checked once against the whole's RTL.

## A ladder of systems

The worked examples climb from the smallest system to the full picture. Each one is the same idea at a
larger size:

| system | the DUT | the testbench | worked example |
|---|---|---|---|
| one kernel on streams | a free-running kernel | its harness: a stream driver and sinks | [Free-running polynomial](../../examples/stream_kernel/index.md) |
| one kernel on a bus | the same kernel as a device, and the crossbar | a two-thread `SwHost` | [Behind AXI-MM](../../examples/stream_kernel/memory_mapped.md) |
| a pipeline with memory | a composite of free-running tasks, memory through stream adaptors | stream drivers, sinks, a memory model | [memcpy](../../examples/memcpy/index.md), [interleaver](../../examples/interleaver/index.md) |
| several kernels, a memory, a host | devices, a memory, the crossbar | a `SwHost` | [Two kernels on a bus](../../examples/markov/index.md) |

The protocol between them is nearly always [command-response](../patterns/command_response.md): a
command and its data in, the results headed by which command they answer, a status per command.

## A system in code

The one-kernel-on-a-bus system, whole ([`stream_kernel_mm.py`](../../../examples/stream_kernel/stream_kernel_mm.py),
shortened): the modules are built on one simulation, then `HwSystem(sim, dut)` names the DUT, and the
testbench -- the host -- is whatever is left.

```python
sim = Simulation()
kernel = PolyKernel(name="poly_kernel", sim=sim, clk=clk)            # the kernel: a FreeRunMod
host = PolyHost(name="host", sim=sim, cmds=cmds, clk=clk)            # the testbench: a SwHost
device = build_mm_device(kernel, sim=sim, clk=clk, mem_dwidth=W)     # the kernel's views and front
xbar = AXIMMCrossBarIF(name="xbar", sim=sim, clk=clk, nports_master=1, nports_slave=len(slaves), ...)
xbar.bind("master_0", host.m)                                        # the host masters the bus
...                                                                   # the device's slaves, interrupts
dut = DutTop.of("poly_mm_dut", sim, comps=[device], ifs=[xbar])
sysm = HwSystem(sim, dut, run_until=host.done, check=check)
```

The one-kernel-on-streams system is one line, because its testbench is generated:

```python
sysm = harness(PolyKernel, workload="test")
```

## Next

- [Building a system](recipe.md): the steps, in the order you take them.
- [The system flow](../build/xsi_system.md#running-it): the build steps from Python to the RTL
  comparison -- `csynth`, `system_rtl`, `scenario`, `pysim`, `system_xsi`, `compare`.
- [Developing module by module](../flows/units.md): harnesses, calibration, composition.
