---
title: Components in XSI
parent: Concurrent (free-running)
grand_parent: Realization Flows
nav_order: 5
audience: python
summary: "What XSI is (the Xilinx Simulator Interface that drives elaborated RTL in xsim from C++), and how a testbench component's participants are represented by XSI BFM models. IMPORTANT: the BFM model classes are hand-written framework; a participant DECLARES which one models it via bfm_model() — nothing auto-generates an XSI class from a component body today."
---

# Components in XSI

<!-- WRITE ME. What XSI is, and how testbench participants map to XSI models. -->

> **Accuracy caution — read before writing.** It is tempting to say "components are compiled to XSI
> classes." **They are not.** The BFM model classes (`AxiMmReadSlave`, `AxiMmWriteSlave`, `AxisMaster`,
> `AxisSlave`, `FlatMemory`, `XsiSim`) are **hand-written framework** in `waveflow/build/xsi/xsi_bfm.h`.
> A testbench participant only **declares which model represents it**, via `bfm_model()` returning a
> `BfmModel("AxisMaster", ...)` — a *name plus constructor args*, not generated code. What *is*
> generated is the wiring (which model drives which RTL port, the phases, the run loop) — that is the
> [next page](./codegen_tb.md). Generating the model classes themselves from a participant's Python body
> is future work (the "step 3" / `XSIParam` direction), not built. Describe the split honestly.

<!-- Suggested content:
     - XSI: xelab -dll -> xsimk.dll; the C++ side drives clocks + reads/writes ports through
       get_port_number / put_value / get_value / run. (See build/xsi.md.)
     - The sample/update/drive phase discipline every model obeys (values sampled before the edge,
       applied after) — load-bearing, from xsi_bfm.h.
     - The participant side: CmdDriver.bfm_model() -> BfmModel("AxisMaster", ports, extra_args);
       WordSink -> "AxisSlave"; the MemComponent -> a shared FlatMemory behind the m_axi slaves. -->

**Source of truth:** `waveflow/build/xsi/xsi_bfm.h` (the model classes), `waveflow/simulation/stream_tb.py`
(`CmdDriver` / `WordSink` and their `bfm_model()`), `waveflow/build/composite_gen.py` (`BfmModel`).
See [Build / XSI](../../build/xsi.md) for the xvlog/xelab/loader mechanics.
