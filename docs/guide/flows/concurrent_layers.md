---
title: XSI simulation components
parent: Concurrent (free-running)
grand_parent: Hardware modules and Flows
nav_order: 1
audience: python
summary: "The concurrent flow uses two simulations: a Python discrete-event simulation for system-level modeling, and an XSI simulation that runs the real RTL cycle by cycle. This page is about the second: what an XSI simulation is made of (the RTL top and the testbench of C++ bus-functional models around it), the three ways an object can be realized in it, the layers inside the RTL top (the Vitis kernel, hand-written Verilog, vendor IP), which side of the bus a kernel can be on, and which parts are generated from the Python graph today."
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

An XSI simulation has two parts:

- **The RTL top** — one Verilog module, the design under test. It is what `xsim` elaborates.
- **The testbench** — every BFM around it, each driving or answering some of the RTL top's pins.

In Python the whole thing is a **testbench graph**: a composite `FreeRunMod` (in the
[mem_copy example](../../examples/memcpy/), `MemCopyTB`) whose children are the design under test and
the participants around it, wired by interfaces exactly as in pysim. `tb_top_spec` walks that graph,
and `render_tb_harness` emits the **harness**: the C++ program that constructs the testbench's models,
binds each to its pins, and runs the cycle loop. Which children are inside the RTL top is decided per
build — that is the [cut](./modules.md#the-cut).

### Three ways to be realized

Every object in the graph must have exactly one cycle-level realization, and there are three. Each is
a declared hook and a target name that `check(obj, target)` answers:

| realization | where | target | the object declares |
|---|---|---|---|
| a task in the Vitis kernel | inside the RTL top | `composite_kernel` | a `run_iter` body, or a hand-written body via [`kernel_task()`](../comp_codegen/freerunning_override.md) |
| hand-written Verilog | inside the RTL top | `rtl_module` | [`rtl_module()`](../comp_codegen/rtl_module.md), naming a `.v` |
| a BFM | the testbench | `xsi_bfm_model` | [`bfm_model()`](../custom_hooks/bfm_model.md), naming a C++ model class |

So an object can be in the **testbench** exactly when it has a BFM — and in principle that is any
object someone is willing to write a cycle model for. The models are `XsiSimObj`s in
[`xsi_bfm.h`](../../../waveflow/build/xsi/xsi_bfm.h), and each implements the same per-cycle
protocol:

| phase | when | what a model does |
|---|---|---|
| `sample()` | clock low | read the design's outputs; decide which handshakes complete this cycle |
| `update()` | after the rising edge | apply those handshakes; advance its state machine |
| `drive()` | after `update` | present its outputs for the next cycle |

Splitting `sample` from `update` is what lets every model see the same cycle: a handshake is decided
from values sampled *before* the edge and applied *after* it, whatever order the models run in.

## Inside the RTL top

The RTL top is itself built from layers, each produced a different way:

```
 testbench .............. C++ BFMs (XsiSimObj), constructed and run by the harness
 │   host (AxiMmMaster) · stream drivers/sinks (AxisMaster/AxisSlave) · memory behind m_axi (FlatMemory)
 │
 └─ RTL top ............. one Verilog module: the design under test, what xsim elaborates
     ├─ Vitis kernel .... csynth's Verilog for the generated ap_ctrl_none top-level function
     ├─ hand-written RTL  memories (bram_t2p) · the memory-mapped adaptor (axi_slave_front + leaves)
     └─ vendor IP ....... AMD's axi_crossbar, generated by create_ip

 (later) block design ... the same RTL top on a board: the PS where the host model was
```

| layer | what is in it | produced by | from |
|---|---|---|---|
| **Vitis kernel** | one `hls::task` per active child, streams, `m_axi` and `bram` ports | `render_top` emits the top-level function, csynth makes it Verilog | the module graph ([`composite_top_spec`](../comp_codegen/freerunning.md)) plus any hand-written task bodies |
| **hand-written RTL** | `waveflow/build/rtl/*.v`: `bram_t2p`, `axi_slave_front`, `mm_queue_in`, `mm_queue_out`, `mm_regbank`, `mm_bram_port` | written once, verified, never generated | `rtl_module()` declares which `.v` a module is |
| **vendor IP** | `axi_crossbar` | `create_ip` in a batch Vivado run, cached by configuration | `AxiXbarConfig` ([`axi_xbar.py`](../../../waveflow/build/axi_xbar.py)) |
| **RTL top** | the three above and the nets joining them | `wrapper_gen` (memories) / `mm_adaptor_gen` (the adaptor) | memories: the graph's `add_rtl_if` edges; the adaptor: example code, for now (see below) |

A design with nothing beside its kernel — [mem_copy](../../examples/memcpy/) is one — has an RTL top
that *is* the Vitis kernel, with no wrapper at all.

## Where each pysim object lands

The same Python class can land in different places in different builds — that is the
[cut](./modules.md#the-cut) — but each kind of object has a usual home:

| pysim object | in the XSI simulation | as |
|---|---|---|
| a `FreeRunMod` kernel (e.g. `MmFir`) | Vitis kernel | an `hls::task` |
| `MemRStream` / `MemWStream` | Vitis kernel | an `hls::task` owning an `m_axi` port |
| `T2pBram` | RTL top, hand-written | `bram_t2p` |
| `MemSlaveWStream` / `MemSlaveRStream` / `MemSlaveRegBank` / `MemSlaveBramWindow` | RTL top, hand-written | an adaptor leaf on the request bus |
| `MemSlaveAdaptor` | RTL top, hand-written + generated | one `axi_slave_front` and a generated decoder |
| `AXIMMCrossBarIF` | RTL top, vendor IP | `axi_crossbar` |
| `MemoryMod` | testbench | `FlatMemory` serving `AxiMmReadSlave` / `AxiMmWriteSlave` |
| a host process holding an `MMIFMaster` | testbench | `AxiMmMaster` |
| `StreamDriver` / `StreamSink` | testbench | `AxisMaster` / `AxisSlave` |

## Which side of the bus a kernel can be on

A kernel can be a bus **master** from inside the Vitis kernel, and can be reached as a bus **slave**
only through the RTL top. The asymmetry is Vitis HLS's, not Waveflow's:

- **Master.** HLS generates `m_axi` ports. `MemRStream` / `MemWStream` own one each and turn a
  command stream into AXI bursts, so a kernel that wants memory sends commands to them. Everything
  stays inside the Vitis kernel. See [Streaming Memory Kernels](../memory/memstream.md).
- **Slave.** HLS generates `s_axilite` — a register file a free-running kernel cannot use, because it
  cannot see a write happen — and **no AXI4-full slave at all**. So a kernel that should be
  *reachable* (a queue a host writes, registers a host sets, a memory a host fills) gets an adaptor
  in the RTL top that turns bus transactions into stream messages. The kernel still sees only
  streams. See [Memory-mapped slave adaptor](../interface/derived/mm_slave.md).

Both sides meet at the crossbar, which is why the crossbar is in the RTL top too: it joins masters in
the Vitis kernel, slaves in the RTL top, and models in the testbench.

## What is generated today, and what is not

Every piece above runs at RTL and is gated. What differs is how much of each **join** comes from the
graph:

- **Module graph → Vitis kernel:** generated. A hand-written task body is declared, not extracted.
- **Vitis kernel + memories → RTL top:** generated. `add_rtl_if` records which kernel port joins
  which memory port, and `wrapper_gen` emits the module. Worked design:
  [A memory reached three ways](../../examples/bram_access/).
- **Adaptor + crossbar → RTL top: assembled by example code.** The pieces are framework —
  `generate_axi_xbar`, `render_view_slot`, `render_adaptor_slot` — but which views exist, at which
  addresses, joined to which kernel ports, is written out in the example
  ([`examples/mm_fir/mm_fir_xsi.py`](../../../examples/mm_fir/mm_fir_xsi.py)), not read off the module
  graph. Teaching `wrapper_gen` to emit it is the first item under *Remaining* in
  `plans/mm_slave_adaptor.md`.
- **Testbench graph → harness:** generated, for stream and memory models. A host *program* — a
  sequence of bus transactions with decisions in it — is written as a C++ state machine over
  `AxiMmMaster` (again in `mm_fir_xsi.py`).

## Running an XSI simulation

[`XsiWorkspace`](../../../waveflow/build/xsi_workspace.py) runs any RTL top through the same
`run.bat` / `run.sh` the example gates use: it copies the BFM headers, writes the `xvlog` file list
(generated IP sources, hand-written Verilog, csynth's Verilog, the RTL top) and the harness, and runs.
It needs no Vitis project for the parts that have none, which is what the adaptor and crossbar gates
use.

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
- [XSI testbench in HLS](../comp_codegen/xsi_tb.md) — how the harness is generated from the testbench
  graph.
- [mm_fir](../../examples/mm_fir/) — every piece in one design: a Vitis kernel, an adaptor and a
  crossbar in the RTL top, a host program in the testbench.
