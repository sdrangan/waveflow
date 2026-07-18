---
title: XSI Build Rung
parent: Build System
nav_order: 6
---

# XSI build rung (from zero)

This page explains the RTL rung used when Vitis cosim is not the right execution path (notably free-running `ap_ctrl_none` task-network tops).

## Terms first

| Term | Plain meaning |
|---|---|
| **RTL / Verilog** | Synthesized hardware description (`*.v`) from `csynth_design`, evaluated cycle-by-cycle by a simulator. |
| **xsim** | Vivado's RTL simulator engine. |
| **xvlog** | Vivado Verilog compiler (`xvlog` compiles Verilog sources for xsim). |
| **xelab** | Vivado elaboration/link step; `xelab -dll` emits a simulator DLL you can load from C/C++. |
| **XSI** | Xilinx Simulator Interface: C/C++ API to drive an xsim DLL (`put_value`, `run`, `get_value`). |
| **BFM** | Bus Functional Model: testbench code that behaves like external bus peers (memory and streams) cycle-by-cycle. |
| **`.f` file** | Text manifest listing Verilog files; passed to `xvlog -f <manifest>`. |

## End-to-end flow

For this rung, the flow is:

`csynth -> Verilog -> xvlog -> xelab -dll -> BFM via XSI -> compare against golden`

Concretely:

1. `csynth_design` generates `solution1/syn/verilog/*.v`.
2. An `.f` manifest (for example `rtl_interleaver_canon.f`) lists those `.v` files.
3. `xvlog -f rtl_<top>.f` compiles the RTL.
4. `xelab work.<top> -dll -s <top>` elaborates and emits a loadable simulator DLL.
5. A C++ BFM executable loads that DLL via XSI and drives AXI-MM + AXI-Stream pins cycle-by-cycle.
6. The BFM compares DUT outputs with the golden model.

## Supplied vs generated vs authored

| artifact | who makes it | authoring reality |
|---|---|---|
| `solution1/syn/verilog/*.v` | Vitis csynth | Fully generated; do not hand-edit. |
| `rtl_<top>.f` | you today (future step later) | Mostly listing generated `.v` paths. |
| `xsi_loader.*`, `xsi_shared_lib.h`, `run.bat` | boilerplate per project | Usually copied/adapted (mostly path/version edits). |
| `*_bfm_tb.cpp` | generated, or you | Generated from the testbench graph when the TB is declared as a component graph (`mem_copy`); hand-assembled for the interleaver tops. Either way it composes framework bus models — it contains no per-cycle handshake code. |

The bus models themselves (`AxisMaster`, `AxiMmReadSlave`, …) are framework code in
`waveflow/build/xsi/xsi_bfm.h`, and scenario data crosses as burst bundles written by Python. See
[BFM Testbenches](./bfm.md).

## Practical Windows/run-script notes

From [`examples/interleaver/xsi/run.bat`](https://github.com/sdrangan/waveflow/tree/main/examples/interleaver/xsi/run.bat):

- The script typically runs `xvlog`, then `xelab -dll`, then `g++`, then executes the BFM EXE.
- `PATH` must include Vivado `bin`, xsim DLL locations, and MinGW toolchain paths.
- Use Windows invocation conventions (for example `.\run.bat <top> <tb_basename>`).
- If running from MSYS/Git Bash, set `MSYS_NO_PATHCONV=1` to avoid path rewriting surprises.

## Codegen vs build/execution responsibilities

- **Codegen responsibility:** produce the top/kernel sources and port shape (see [schema HLS codegen](../schema/hls/codegen.md) and related build codegen pages).
- **Build/execution responsibility (this page):** compile generated RTL and execute it in simulation (`xvlog`/`xelab`/XSI/BFM).

Codegen defines *what* gets built; this rung defines *how that RTL is executed and checked*.

## Automation status (current and future)

Be explicit about maturity:

- `csim` / `csynth` / `cosim` are represented as documented build-step patterns in the BuildDag flow.
- The XSI rung is currently a standalone script flow (`run.bat` + BFM), not a full BuildDag `BuildStep` equivalent yet.
- A future `XsiStep` is a reasonable direction (generate `.f`, invoke `xvlog`/`xelab`/compiler, run BFM), but that is aspirational.

## See also

- [Build System index](./index.md) — one flow, fork at the RTL rung.
- [BFM Testbenches](./bfm.md) — the bus models, the five-phase lifecycle, and how a TB is assembled.
- [schema HLS codegen](../schema/hls/codegen.md) — generating the C++/HLS side consumed by build steps.
