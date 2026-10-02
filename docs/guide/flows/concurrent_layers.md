---
title: XSI simulation components
parent: Concurrent (free-running)
grand_parent: Hardware modules and Flows
nav_order: 1
audience: python
summary: "The concurrent flow uses two simulations: a Python discrete-event simulation for system-level modeling, and an XSI simulation that runs the real RTL cycle by cycle. This page is the big picture of the second: an XSI simulation top made of the RTL top (what will be synthesized) and a testbench of C++ bus-functional models, the three ways an object can be realized in it, and where each kind of pysim object usually lands."
---

# XSI simulation components

## Python DES and XSI simulation

A concurrent design is typically simulated in two quite different ways.

- **Python DES (pysim).** A discrete-event simulation of any collection of `SimObj`s: the
  `HwModule`s of the design, and anything else the evaluation needs. A channel estimator, for example,
  can be simulated together with a wireless channel model and a traffic source. Time advances from
  event to event, not clock by clock, so it runs fast, and a module can be described at whatever
  level of detail the question needs. It is not meant to be cycle-accurate. It is where a design is
  explored, compared, and evaluated as part of a larger system.

- **XSI simulation.** The hardware itself — the Verilog that Vitis synthesizes, plus any hand-written
  Verilog beside it — simulated cycle by cycle in Vivado's simulator, `xsim`. *XSI* (the Xilinx
  Simulator Interface) loads the compiled design as a shared library, so a C++ program can set its
  input pins, advance the clock, and read its output pins. Everything outside the hardware is a
  **bus-functional model (BFM)**: a C++ cycle model of one participant — a host, a stream source, a
  memory — that speaks the pin-level protocol (AXI-Stream valid/ready handshakes, AXI4 bursts) on
  every cycle.

Why XSI rather than Vitis C/RTL co-simulation: co-simulation drives a kernel through its
`ap_start` / `ap_done` handshake, and a free-running kernel has none — it never starts and never
returns. Driving the pins directly works for any design, measures exact cycle counts, and lets the
models around the hardware run concurrently with it, as they would on a board.

The price is that an XSI simulation is more restricted and slower than pysim: **every** object in it
needs a cycle-level realization. The rest of this page is about what those realizations are and how
they fit together.

## What an XSI simulation is made of

An XSI simulation has a single composite `FreeRunMod` that defines the **XSI simulation top**. In
the [mem_copy example](../../examples/memcpy/), for instance, the XSI simulation top is `MemCopyTB`.
Its components fall into two groups:

- **The RTL top** — one Verilog module, the design under test, and what `xsim` elaborates. These
  modules are what will actually be synthesized: the same hardware can be packaged as one or more IP
  kernels and included in, say, a Vivado project.
- **The testbench** — every BFM around the RTL top, each driving or answering some of its pins. These
  modules are generally not intended for synthesis.

Which children are inside the RTL top is decided per build — that is the
[cut](./modules.md#the-cut). How the composite becomes a running simulation is
[Generating the XSI simulation](./concurrent_codegen.md).

### Three kinds of module

Every module in the XSI simulation top plays exactly one of three **roles**, and each role has its own
realization in the XSI simulation:

| Python role | the module declares | target | in the XSI simulation |
|---|---|---|---|
| **kernel module** | a `run_iter` body, or a hand-written body via [`kernel_task()`](../comp_codegen/freerunning_override.md) | `composite_kernel` | a task in the **Vitis kernel**: generated HLS C++, synthesized to Verilog |
| **RTL module** | [`rtl_module()`](../comp_codegen/rtl_module.md), naming a `.v` | `rtl_module` | **hand-written Verilog**, used as is |
| **BFM module** | [`bfm_model()`](../custom_hooks/bfm_model.md), naming a C++ model class | `xsi_bfm_model` | a **C++ BFM** |

Kernel modules and RTL modules make up the RTL top; BFM modules make up the testbench.

A role is not a property of a class. The same class can be a kernel module in one build and a BFM
module in another — which is the [cut](./modules.md#the-cut).

`check(obj, target)` reports whether a module can take a given role.

So a module can be a **BFM module** exactly when it has a C++ BFM — and in principle that is any
module someone is willing to write a cycle model for. The BFMs are `XsiSimObj`s in
[`xsi_bfm.h`](../../../waveflow/build/xsi/xsi_bfm.h), and each implements the same per-cycle
protocol:

| phase | when | what a model does |
|---|---|---|
| `sample()` | clock low | read the design's outputs; decide which handshakes complete this cycle |
| `update()` | after the rising edge | apply those handshakes; advance its state machine |
| `drive()` | after `update` | present its outputs for the next cycle |

Splitting `sample` from `update` is what lets every model see the same cycle: a handshake is decided
from values sampled *before* the edge and applied *after* it, whatever order the models run in.

## Where each pysim object lands

Each kind of object has a usual role:

| pysim object | usual role | in the XSI simulation |
|---|---|---|
| a `FreeRunMod` kernel (e.g. `MmFir`) | kernel module | an `hls::task` in the Vitis kernel |
| `MemRStream` / `MemWStream` | kernel module | an `hls::task` owning an `m_axi` port |
| `T2pBram` | RTL module | `bram_t2p` |
| `MemSlaveWStream` / `MemSlaveRStream` / `MemSlaveRegBank` / `MemSlaveBramWindow` | RTL module | an adaptor leaf (`mm_queue_in`, ...) |
| `MemSlaveAdaptor` | RTL module | one `axi_slave_front` and a generated decoder |
| `MemoryMod` | BFM module | `FlatMemory` serving `AxiMmReadSlave` / `AxiMmWriteSlave` |
| `StreamDriver` / `StreamSink` | BFM module | `AxisMaster` / `AxisSlave` |
| a host process holding an `MMIFMaster` | BFM module | `AxiMmMaster` |

One thing in an RTL top is not a module: the AXI crossbar. In pysim it is an interface,
`AXIMMCrossBarIF`, joining modules; in the RTL top it becomes vendor IP, AMD's `axi_crossbar`,
generated by `create_ip` — beside the hand-written Verilog, not part of it.

## Moving the cut: what it costs today {#moving-the-cut}

Which modules are inside the Vitis kernel is [a property of the build, not of the
class](./modules.md#the-cut) — so in principle a module moves between the kernel and the testbench by
changing only the cut. The capability is real in the **graph** and not yet real in the **artifact**,
and it is worth being precise about which is which.

`MemRStream` genuinely is generated at two cuts: as its own top
([`examples/interleaver/gen/mem_r_stream.cpp`](https://github.com/sdrangan/waveflow/tree/main/examples/interleaver/gen/mem_r_stream.cpp),
XSI gate **158**) and as a task inside `mem_copy`
([`examples/mem_copy/gen/mem_copy.cpp`](https://github.com/sdrangan/waveflow/tree/main/examples/mem_copy/gen/mem_copy.cpp),
gate **2908**). But those two are **two protocols**, not one module at two cuts: the standalone one
reads an `MRCmd` and bursts; the composite one reads a `MemRCmd` and relays `fwd_bursts` opaque
bursts first. `inband` is a `HwParam` — a build-time parameter of the *design* — precisely because it
selects a protocol, and the framing follows from the protocol rather than the other way round.

Holding the protocol fixed and moving *only* the cut does not work yet. Ask the generator for the
in-band reader as a standalone top and it will emit this:

```cpp
void mem_r_stream(hls::stream<ap_uint<64> >& s_cmd, ...) {          // plain words at the boundary
    hls_thread_local hls::task t0(mem_r_stream_framed_task<64>, s_cmd, m_mem, m_out);
}                                          // ...but the body's signature demands framed_word<64>
```

That does not compile, and nothing in Python catches it. The task body's argument word types are not
part of `kernel_task()`'s contract, so the generator cannot check them — and the obvious proxy does
not work either: `mem_copy`'s own `s_done` endpoint is `has_tlast=True` in Python while
`mem_w_stream_framed_done_task` declares it a plain `ap_uint` stream, and that design is the 2908
gate. The Python framing flag and the C++ word type already disagree on a *working* design.

Making the cut free in the artifact means teaching `kernel_task()` about the cut. That is designed
but not built — see `plans/design_cut.md` §S5.

## See also

- [Memory-mapped slave adaptor](../interface/derived/mm_slave.md) — the adaptor's views, their
  semantics, and the ordering guarantee.
- [MM Interfaces](../interface/primitive/aximm.md#how-it-lowers) — `AXIMMCrossBarIF` in pysim, and
  `axi_crossbar` at RTL.
- [Generating the XSI simulation](./concurrent_codegen.md) — how the XSI simulation top becomes a
  harness, and how a simulation is run.
- [mm_fir](../../examples/mm_fir/) — every piece in one design: a Vitis kernel, an adaptor and a
  crossbar in the RTL top, a host program in the testbench.
- [Memory-mapped slave adaptor](../interface/derived/mm_slave.md#the-structure) — why a kernel can
  drive the bus from inside the Vitis kernel but is reached through an adaptor in the RTL top.
