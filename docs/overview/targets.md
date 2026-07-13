---
title: Build targets
parent: Overview
nav_order: 4
---

# What you build from the Python model

[The Python model](./pymodel.md) makes the point that a Waveflow component is a *specification*, and
the HLS kernel is only one of its **outputs**. This page enumerates the full set — the **targets** you
can realize from that one model, from a fast behavioral simulation up to (eventually) a bitstream — and
what each is for.

The targets form a **ladder of fidelity**: cheapest and fastest at the bottom, cycle- and
resource-**exact** at the top. You climb from the cheap end and drop to a higher rung only when the one
below can't express the design. [The Waveflow flow](./flow.md) is the *methodology* that moves between
them (the two loops and their calibration bridge); this page is the *catalog*.

A target is a **path, not a tool.** "Behavioral simulation" isn't one module — producing it draws on the
[schemas](../guide/schema/) (the payloads), the [interfaces](../guide/interface/) (the transactions),
the [components](../guide/components/) (the behavior), and the [simulator](../guide/sim/) (the runtime).
Each row names the parts it leans on.

| Target | What it is | Fidelity | Draws on | Status |
|---|---|---|---|---|
| **Behavioral** (PySim) | the [SimPy](../guide/sim/) discrete-event sim runs the model directly | bit-exact data, cycle-*approximate* (calibrated) timing | schema · interface · components · sim | **Built** |
| **Vitis C-sim** | the generated C++ kernel + testbench, compiled and run functionally (no RTL) | functional only (no timing) | [codegen](../guide/comp_codegen/) · [build](../guide/build/) | **Built** |
| **Vitis co-sim** | the kernel synthesized to RTL and co-simulated against the *same* C++ testbench | cycle/resource-**exact**, one `ap_ctrl_hs` kernel | + RTL synth | **Built** (cosim-able kernels) |
| **xsim** (XSI / SystemC) | RTL of a free-running or multi-block design, driven by a C++/SystemC BFM in Vivado's simulator | cycle-exact; reaches what co-sim **can't** | [build/xsi](../guide/build/xsi.md) · [bfm](../guide/build/bfm.md) | **Built** (XSI); SystemC partial |
| **Bitstream** (Vivado IPI) | the whole system as a block design (kernels as IP + interconnect + real vendor IP + memory) → synth/impl → `.bit` + `.xsa` | the real FPGA | future IPI flow | **Roadmap** |

The middle three rungs (C-sim → co-sim → xsim) are the **verification ladder**; its mechanics — the
`BuildDag` steps, the TCL, and the XSI harness — live in the [Build System](../guide/build/). This page
is just the wider view, adding the behavioral rung below and the bitstream above.

## Not every block realizes the same way

A target is a *column*; a **block is a row** — and the two don't line up uniformly. A synthesizable
datapath is *real* at every rung from C-sim up. But other roles realize differently:

- a **testbench** *drives* the simulation rungs and then **vanishes** at the bitstream — the real
  environment replaces it;
- **vendor IP** like the RFSoC data converter (**RFDC**) can never be C-sim'd or synthesized by Waveflow;
  it is a **behavioral model** at every simulation rung and the **real IP** only in the bitstream;
- an **AXI interconnect** is **implicit** in simulation (it's just the [`Interface`](../guide/interface/)
  wiring) and becomes a **generated IP** only in the bitstream.

So the same Python object carries different realizations, and Waveflow picks the right one per target —
the single-source-of-truth idea extended past synthesizable kernels to the whole system. The by-block
view of this — every role and how it realizes down the ladder — is the
[component taxonomy](../guide/components/taxonomy.md).

## What's built, and what's coming

Everything from **behavioral simulation through xsim** is built and validated bit-for-bit on individual
modules (see [Project status](./status.md)). The **entire bitstream / IPI column is roadmap** — real
RFDC, a generated interconnect, and the block-design flow are the [SALSA](./salsa.md) / RFSoC bring-up
direction, not yet built. A "partial" on the xsim rung means the harness is proven in isolation but not
yet integrated across a full multi-block system.
