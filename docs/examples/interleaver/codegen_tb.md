---
title: Testbench codegen
parent: Interleaver (gather)
nav_order: 5
---

# Testbench codegen

The [DUT codegen](./codegen_dut.md) lowered the design graph to an RTL top that is **free-running**
(`ap_ctrl_none`): it owns its two `m_axi` bundles and has no `ap_ctrl` start/done handshake. Vitis
C/RTL cosim can only drive a kernel it can *call* — so it cannot drive this top at all. The testbench is
instead a C++ **BFM** (bus-functional model) that drives the synthesized RTL directly in Vivado `xsim`,
one clock at a time, through [XSI](../../guide/build/bfm.md). This page is how that testbench is
generated — from the `InterleaverInbandTB` graph in `interleaver_inband_sim.py`, the RTL-side counterpart
to the [Python golden](./testbench.md). That harness (`InterleaverInbandSim`) drives *its own* pysim run and
the XSI bundles from one `write_scenario`, so the RTL scenario cannot drift from the model it is checked
against.

## What generate_tb emits

[`generate_tb`](../../../examples/interleaver/interleaver_inband.py) walks the
`InterleaverInbandTB` graph — via `make_xsi_tb` → `tb_top_spec` — and writes three files under `xsi/`:

- **`interleaver_inband_vectors.h`** — the scenario constants (`render_xsi_vectors`): `MEM_DW`, the
  arena size `MEM_NW`, the command count `NUM_CMDS`, and `DONE_WORDS` (words per echoed `IlDesc`).
  These are the sizes the harness needs; the per-job `p_off`/`x_off`/`y_off` offsets themselves ride
  the `vectors/s_cmd` bundle, not the header.
- **`interleaver_inband_tb_harness.h`** — the BFM models, their wiring to the RTL ports (via the DUT's
  `ports.h` from [DUT codegen](./codegen_dut.md)), the lifecycle phases, and the fixed-cycle run loop
  (`render_tb_harness`).
- **`interleaver_inband_bfm_tb.cpp`** — the whole `main`, a construct-run-close over `tb.n_cycles`
  (`render_tb_main`). There is **no golden in the C++**: it runs and it dumps.

The scenario is parameterized by the job-size tuple `sizes` — one `InterleaverCmd` per job, of possibly
different lengths (variable-length gather). `sizes` sets `NUM_CMDS` and the arena layout `MEM_NW`, so the
same generator emits a fixed-size `(256,)` bench or a mixed `(256, 128, 64)` one from one call.

## The BFM: each participant drives real handshakes

Every participant on a boundary port of the DUT maps to its C++ **BFM twin**, declared by the
participant itself (`bfm_model()`), not by a table. For the interleaver that is:

| pysim participant | XSI model | drives |
|---|---|---|
| `StreamDriver` | `AxisMaster` | the `s_cmd` AXI-Stream — offers each `InterleaverCmd` word |
| `MemComponent` | `FlatMemory` + `AxiMmReadSlave` / `AxiMmWriteSlave` | the two `m_axi` bundles |
| `StreamSink` | `AxisSlave` | the `s_done` AXI-Stream — always ready, keeps the echoed descriptors |

The `AxisMaster` is a cycle-level AXI-Stream *master* feeding `s_cmd` the command words. Behind the two
`m_axi` bundles the memory expands to **three** C++ objects: one `FlatMemory` arena holding `P`/`X` (and
capturing `Y`), plus a slave BFM per bundle — an `AxiMmReadSlave` for `m_in`/`gmem0` (serves the `P` and
`X` reads) and an `AxiMmWriteSlave` for `m_out`/`gmem1` (absorbs the `Y` writes). In pysim the crossbar
is one interface; at RTL there is no crossbar, so each bundle needs its own slave and both serve the same
arena — the generator works that out from the graph. The `AxisSlave` on `s_done` drains the commit-timed
completions the way the pysim sink does. Each model obeys the same `sample` / `update` / `drive` phase
split as pysim's lifecycle, so a beat decided from values sampled *before* the rising edge is applied
*after* it. The BFM library (`waveflow/build/xsi/xsi_bfm.h`) is **framework** — it models AXI4 /
AXI4-Stream and knows nothing about the interleaver.

## write_xsi_bundles: the scenario on disk

`write_xsi_bundles` materializes the input and golden bundles into `xsi/vectors/`, delegating to
`InterleaverInbandSim.write_scenario` — the one writer `InterleaverInbandSim` uses for both its own pysim
run and the XSI bundles:

- **`vectors/s_cmd`** — the serialized `InterleaverCmd` words the `AxisMaster` streams in;
- **`vectors/mem_in`** — the flat arena with `P` and `X` placed at each job's offsets, loaded into the
  `FlatMemory`;
- **`vectors/golden`** — the expected `Y = X[P]`, for the check (not consumed by the C++).

The run itself writes **`vectors/out`** (the whole arena dumped from the `FlatMemory`, so the captured
`Y` regions) and **`vectors/s_done`** (what the `AxisSlave` collected). Every value crossing the boundary
is a burst bundle.

## The harness assembly: XsiHarnessStep

The generated `*.h`/`*.cpp` are only the example-specific half. The reusable XSI flow is copied in beside
them by [`XsiHarnessStep`](../../../waveflow/build/streamutils.py) — the same framework files for every
XSI bench:

- **`run.bat`** — the build/run script: `xvlog` compiles the RTL, `xelab` elaborates it into the
  `xsimk` shared library, `g++` compiles the BFM `main` against `xsi_loader`, then `xsim` runs it;
- **`xsi_bfm.h`** — the BFM model library above;
- **`xsi_loader.h` / `xsi_loader.cpp` / `xsi_shared_lib.h` / `xsi_bundle.h`** — the XSI shared-library
  loader and the burst-bundle I/O the C++ side reads/writes.

The RTL file list `run.bat` hands `xvlog` is emitted separately by `render_rtl_f`
(`xsi/rtl_interleaver_inband.f`), after csynth, since it names the synthesized `.v` files.

## check_xsi_outputs: the same golden, through real RTL

`check_xsi_outputs` reads the two dumped bundles back in Python and applies the **same** golden as the
pysim testbench — now proven through real RTL. Per job it slices the captured `Y` region out of
`vectors/out` and asserts it equals `vectors/golden` bit-exact (the `Y[i] = X[P[i]]` gather), reporting
the first mismatching word if not. It then checks `vectors/s_done` carries exactly `NUM_CMDS × DONE_WORDS`
words — one commit-timed completion per job. Correctness lives in Python at both ends; the C++ only moves
cycles.

## The generated-TB pattern

This is the same shape `mem_copy` uses — its [Testbench codegen](../memcpy/codegen_tb.md) page is the
template. The only hand-written half of the whole RTL testbench is two Python functions:
`write_xsi_bundles` (a wrapper over `write_scenario`) and `check_xsi_outputs` (the golden). Everything
else — harness, `main`, the port map, the BFM models — is generated or framework. Python writes the
inputs, the kernel and testbench are generated, the toolchain runs them, Python checks the outputs.

## Next

[RTL simulation](./rtlsim.md) — synthesize the top, run the harness through XSI, and compare the cycle
count to pysim.
