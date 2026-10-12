---
title: XSI Build Rung
parent: Build System
nav_order: 6
summary: "The RTL rung for when Vitis cosim is not the right path — notably free-running ap_ctrl_none task networks, which cosim refuses to run. Defines the terms first (xsim, xvlog, xelab, XSI, BFM, the .f manifest), then walks the flow from csynth through a simulator DLL driven cycle by cycle from C++ and compared against a golden."
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
| `xsi_loader.*`, `xsi_shared_lib.h`, `run.bat` / `run.sh` | framework (`waveflow/build/xsi/`) | Copied into each workspace by the build; do not hand-edit. |
| `*_bfm_tb.cpp` | generated, or you | Generated from the testbench graph when the TB is declared as a component graph (`mem_copy`); hand-assembled for the interleaver tops. Either way it composes framework bus models — it contains no per-cycle handshake code. |

The bus models themselves (`AxisMaster`, `AxiMmReadSlave`, …) are framework code in
`waveflow/build/xsi/xsi_bfm.h`, and scenario data crosses as burst bundles written by Python. See
[BFM Testbenches](./bfm.md).

## Practical Windows/run-script notes

From [`examples/interleaver/xsi/run.bat`](https://github.com/sdrangan/waveflow/tree/main/examples/interleaver/xsi/run.bat):

- With no verb the script runs `xvlog`, then `xelab -dll`, then `g++`, then executes the BFM EXE; a
  verb selects phases (see [Building once, running many times](#building-once-running-many-times)).
- `PATH` must include Vivado `bin`, xsim DLL locations, and MinGW toolchain paths.
- Use Windows invocation conventions (for example `.\run.bat <top> <tb_basename>`).
- If running from MSYS/Git Bash, set `MSYS_NO_PATHCONV=1` to avoid path rewriting surprises.

## Codegen vs build/execution responsibilities

- **Codegen responsibility:** produce the top/kernel sources and port shape (see [schema HLS codegen](../schema/hls/codegen.md) and related build codegen pages).
- **Build/execution responsibility (this page):** compile generated RTL and execute it in simulation (`xvlog`/`xelab`/XSI/BFM).

Codegen defines *what* gets built; this rung defines *how that RTL is executed and checked*.

## Building once, running many times

An XSI run is four phases: compile the RTL (`xvlog`), elaborate it into a design library (`xelab
-dll`), compile the testbench (`g++`), and simulate. On the examples the first three take 20–30 s
together and the simulation a fraction of a second — so re-running a design that has not changed
should cost only the last one. Three pieces make it so.

**The runner takes a verb.** `run.bat` / `run.sh` accept, in any order after the top and testbench
names, `trace`, one of the verbs below, and a vectors directory:

| verb | phases |
|---|---|
| `rtl` | compile the RTL and elaborate the snapshot |
| `tb` | compile and link the testbench |
| `build` | `rtl` + `tb` |
| `run` | run the testbench that is built, against the snapshot that is elaborated |
| `all` (or none) | every phase, unconditionally — what the `-m xsi` gates use |

```bat
.\run.bat mem_copy mem_copy_bfm_tb build
.\run.bat mem_copy mem_copy_bfm_tb run runs\p3
```

Each phase deletes its own outputs before it rebuilds them, so a failed phase cannot leave an older
artifact behind. With `trace`, the design is elaborated with the VCD dumper as a second top into
its own snapshot, `xsim.dir/<top>_trace`, so a traced and an untraced build coexist. Every run of
the traced snapshot writes `<top>_trace.vcd`; a run of the untraced one never does.

**`XsiSnapshot` decides what to rebuild.** The runner never decides whether a phase is needed;
[`waveflow.build.xsi_snapshot.XsiSnapshot`](https://github.com/sdrangan/waveflow/tree/main/waveflow/build/xsi_snapshot.py)
does, by content:

```python
from waveflow.build.xsi_snapshot import XsiSnapshot

snap = XsiSnapshot(xsi_dir, top="mem_copy", tb="mem_copy_bfm_tb")
snap.stale()        # ["rtl", "tb"], a subset, or [] -- what a build would run
snap.build()        # runs only the stale phases; returns them
out = snap.run(vectors_dir=xsi_dir / "runs" / "p3")   # build (incrementally), then run
```

After a phase succeeds it writes a stamp: a SHA-256 of every input the phase read. For the design,
that is the runner script, `rtl_<top>.f`, every file it lists and every file of each `--include`
directory (and, traced, the dumper). For the testbench, it is the runner, `<tb>.cpp`,
`xsi_loader.cpp`, every quoted `#include` they reach, and `WF_TB_CXXFLAGS`. A phase is skipped only
when its stamp matches and its output exists. Timestamps are not used: a regenerated file with
identical bytes is still fresh, and an edit that is undone is fresh again.

The design's stamp lives inside `xsim.dir/<snapshot>/`, so deleting the snapshot — which the gates
do to force a clean build — deletes its stamp too. [`RtlSimStep`](../timing/trace_steps.md),
`XsiWorkspace` (and with it [`system_xsi`](./xsi_system.md)), and the vendor-block RTL runners all
go through `XsiSnapshot`.

**A run reads its scenario from a directory you choose.** Testbenches name their bundles
`vectors/<port>`. With a vectors directory — the runner's last argument, or `run(vectors_dir=...)`
— the BFM library reads and writes those bundles there instead (`WF_VECTORS_DIR`, resolved in
`xsi_bundle.h`), and puts the run's waveform database there too. So two runs of one snapshot do not
overwrite each other's outputs, and they can run in parallel: build once, then call
`run(d, build=False)` from several threads. Eight parallel `mem_copy` runs took 0.27 s in total, and
each wrote outputs byte-identical to a single run's.

A scenario's *sizes* are part of the scenario too. The cycle bound of the generated `main` and the
size of a memory arena are compiled into the testbench as defaults, and a run's `vectors/run.json`
overrides them (`wfbfm::run_param`; written by `waveflow.utils.burst_io.write_run_params`). A run
that overrides one prints `WF_RUN_PARAM <key>=<value>`. This is what lets one compiled testbench
serve every workload: [`mem_copy`'s workload sweep](https://github.com/sdrangan/waveflow/tree/main/examples/mem_copy/mem_copy_workload_sweep.py)
runs fifty job lengths at RTL in 36 s, each point checked bit-exact, at about 0.5 s a point
(0.3 s of it simulating) — against 20–30 s a point when every run rebuilt everything.

{: .note }
> A run reads whatever `run.json` its vectors directory holds. A scenario writer that owns a
> `vectors/` should therefore write one every time, even at the default scenario, so a previous
> point's sizes cannot outlive it — `MemCopySim.write_scenario` does.

## See also

- [Build System index](./index.md) — one flow, fork at the RTL rung.
- [BFM Testbenches](./bfm.md) — the bus models, the five-phase lifecycle, and how a TB is assembled.
- [schema HLS codegen](../schema/hls/codegen.md) — generating the C++/HLS side consumed by build steps.
