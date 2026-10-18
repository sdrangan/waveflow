---
title: Build targets
parent: Overview
grand_parent: Waveflow
nav_order: 4
---

# What you build from the Python model

[The Python model](./pymodel.md) makes the point that a Waveflow module is a *specification*, and
its hardware is one realization of it, checked against it. This page enumerates the full set — the **targets** you
can realize from that one model, from a fast behavioral simulation up to (eventually) a bitstream — and
what each is for.

The targets form a **ladder of fidelity**: cheapest and fastest at the bottom, cycle- and
resource-**exact** at the top. You climb from the cheap end and drop to a higher rung only when the one
below can't express the design. [The Waveflow flow](./flow.md) is the *methodology* that moves between
them (the two loops and their calibration bridge); this page is the *catalog*.

A target is a **path, not a tool.** "Behavioral simulation" isn't one module — producing it draws on the
[schemas](../guide/schema/) (the payloads), the [interfaces](../guide/interface/) (the transactions),
the [modules](../guide/flows/modules.md) (the behavior), and the [simulator](../guide/sim/) (the runtime).
Each row names the parts it leans on.

| Target | What it is | Fidelity | Draws on | Status |
|---|---|---|---|---|
| **Behavioral** (PySim) | the [SimPy](../guide/sim/) discrete-event sim runs the model directly, beside anything else the question needs ([Simulating in Python](../guide/systems/pysim.md)) | bit-exact data, cycle-*approximate* (calibrated) timing | schema · interface · modules · sim | **Built** |
| **csynth** | each HLS top (the generated top around the kernel's HLS body) synthesized by Vitis | resource and clock **estimates** per top | [codegen](../guide/comp_codegen/) · [build](../guide/build/) | **Built** |
| **xsim** (XSI) | the DUT's RTL in an `HwSystem(sim, dut)` — a single module in its harness, or a whole system with a host — under Vivado's simulator, every testbench part as its C++ twin ([Simulating at RTL](../guide/systems/rtl.md)) | cycle-**exact** | [build/xsi](../guide/build/xsi.md) · [bfm](../guide/build/bfm.md) · [system](../guide/build/xsi_system.md) | **Built** |
| **Block design** (Vivado IPI) | each device and memory packaged as IP and wired with AMD's crossbar in a validated block design, from the same spec as the XSI top ([Toward the board](../guide/flows/bitstream_ipi.md)) | system resources from Vivado, globally or per device | [ipi](../guide/build/ipi.md) | **Built** (packaging + `validate_bd_design`) |
| **Bitstream** | the block design with the processor, the board's part and real vendor IP → synth/impl → `.bit` + `.xsa` | the real FPGA | IPI flow | **Roadmap** |

The two simulation rungs, pysim and xsim, are the **verification ladder**: the RTL run is compared with
pysim word for word ([Functional verification](../guide/systems/verification.md)); its mechanics — the
`BuildDag` steps, the TCL, and the XSI system top — live in the [Build System](../guide/build/). (The older
rungs, Vitis C-simulation and C/RTL co-simulation of one `ap_ctrl_hs` kernel against a sequential C++
testbench, are kept under [Legacy flows](../guide/legacy/index.md).) This page is just the wider view,
adding the board above.

## Not every block realizes the same way

A target is a *column*; a **block is a row** — and the two don't line up uniformly. A synthesizable
datapath is *real* at every rung from csynth up. But other roles realize differently:

- a **testbench** *drives* the simulation rungs (in pysim as Python, at RTL as its C++ twins) and then
  **vanishes** at the bitstream — the real environment replaces it;
- **vendor IP** like the RFSoC data converter (**RFDC**) can never be synthesized by Waveflow;
  it is a **behavioral model** at every simulation rung and the **real IP** only in the bitstream;
- an **AXI interconnect** is a **model** in pysim (the crossbar's address ranges) and AMD's crossbar
  **IP**, generated from those ranges, at RTL and in the block design.

So the same Python object carries different realizations, and Waveflow picks the right one per target —
the single-source-of-truth idea extended past synthesizable kernels to the whole system. The by-block
view of this — every role and how it realizes down the ladder — is the
[module taxonomy](../guide/flows/modules.md).

## What's built, and what's coming

Everything from **behavioral simulation through xsim** is built and validated bit-for-bit, on individual
modules and on multi-module systems with a host (see [Project status](./status.md)), and so are IP
packaging and a validated block design. The **bitstream is roadmap** — the processor in the block
design, the board's part, real RFDC, and host software on the board are the [SALSA](./salsa.md) /
RFSoC bring-up direction, not yet built ([Toward the board](../guide/flows/bitstream_ipi.md#status)).
