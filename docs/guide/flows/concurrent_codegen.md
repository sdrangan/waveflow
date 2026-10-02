---
title: Generating the XSI simulation
parent: Concurrent (free-running)
grand_parent: Hardware modules and Flows
nav_order: 3
audience: python
summary: "How the XSI simulation top becomes something xsim can run: tb_top_spec checks the composite and describes which C++ BFM sits on which pins, render_tb_harness renders that as the harness, and the Vitis kernel is generated separately from the kernel modules. Then how a simulation is run, including designs with no Vitis project."
---

# Generating the XSI simulation

[XSI simulation components](./concurrent_layers.md) describes what an XSI simulation is made of: the
**XSI simulation top** — a composite `FreeRunMod` — whose children are kernel modules and RTL modules
(which make the RTL top) and BFM modules (which make the testbench). This page is how that composite
is turned into a running simulation.

## From the XSI simulation top to a harness

The XSI simulation top needs no special attribute; what makes it one is what two framework functions
in [`composite_gen.py`](../../../waveflow/build/composite_gen.py) can do with it. (The code calls this
composite the testbench — `tb_top_spec`, `TbSpec` — so read `tb` there as "XSI simulation top".)

- **`tb_top_spec(top, dut=None)` checks it and describes it.** It picks the kernel side — the child
  named by `dut=`, or else the one child with a `boundary` — and walks that child's boundary ports.
  For each port it finds the BFM module wired to it and resolves its C++ BFM, refusing the design if
  a BFM module declares none, names a model class that does not exist, or leaves a port uncovered.
  The result, a `TbSpec`, is plain data: which model sits on which pins.
- **`render_tb_harness(spec)` renders that description** as the **harness**: the C++ that constructs
  the testbench's models, binds each to its pins, and runs the cycle loop. It checks nothing and
  generates no model code — the BFMs are pre-written, and the harness only instantiates and wires
  them.

The Vitis kernel is generated separately, from the kernel modules, by `composite_top_spec` and
`render_top`. The full walk is the [XSI testbench](../comp_codegen/xsi_tb.md) page.

A BFM module that is a *program* rather than a protocol model — a host that reads a status
register and decides what to write next — is not generated today. It is written as a C++ state
machine over the `AxiMmMaster` BFM; [mm_fir](../../examples/mm_fir/rtlsim.md#the-host-program) is the
worked case.

## Running an XSI simulation

[`XsiWorkspace`](../../../waveflow/build/xsi_workspace.py) runs any RTL top through the same
`run.bat` / `run.sh` the example gates use: it copies the BFM headers, writes the `xvlog` file list
(generated IP sources, hand-written Verilog, csynth's Verilog, the RTL top) and the harness, and runs.
It needs no Vitis project for the parts that have none, which is what the adaptor and crossbar gates
use.
