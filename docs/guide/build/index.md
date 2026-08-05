---
title: Build System
parent: Guide
nav_order: 9
has_children: true
---

# Build System

Waveflow's build system models the full path from a Python design to its Vitis HLS outputs as a directed acyclic graph of typed steps. Each step declares the named artifacts it **consumes** and **produces** — files on disk *or* in-memory Python objects — and the DAG wires dependencies automatically, runs steps in topological order, propagates failures, and skips steps whose outputs are already fresh. The result is a single Python pipeline that can carry a design from schema declaration through code generation, Python simulation, Vitis C-sim, C-synth, and report inspection, with incremental rebuilds and one source of truth for what gets built and why.

## One flow, with a fork at the RTL rung

There is one build flow and one fidelity ladder. The front half is shared:

`schema/codegen -> pysim golden -> csim -> csynth`

The flow forks only at the final RTL-exercising rung, based on top execution model:

| top kind | RTL rung |
|---|---|
| single-kernel top (`ap_ctrl_hs`, register-map launched) | [Vitis cosim](./vitis.md) |
| composite free-running task-network top (`ap_ctrl_none`) | [XSI + BFM](./xsi.md) |

That fork is two realizations of one flow, not two separate flows.

## Why it's useful

- **One DAG, one command.** `dag.run(config)` executes every step in the right order. No shell scripts, no make.
- **Mixed file / in-memory artifacts.** A `PySimStep` can produce a Python object; a downstream `ExtractPyTimingStep` consumes it directly without serialising to disk.
- **Auto-wired dependencies.** Steps declare `consumes = [...]` and `produces = {...}`; the DAG figures out who depends on whom. No manual `dep_step.add_dependent(self)` calls.
- **Incremental rebuilds.** File freshness is checked by mtime; only stale steps re-run. `force=True` or `force=["step_name"]` overrides when needed.
- **Subgraph execution.** `dag.run(config, through="csim")` runs everything required by `csim` and stops.
- **Introspectable.** `dag.info()` / `dag.describe()` / `dag.results_status()` give machine- and human-readable views of the graph and freshness state.
- **Progress callbacks.** `on_step_begin` / `on_step_end` hooks let CLIs print status without each step having to know about logging.

## Topics

- [Core Components](./corecomp.md) — `BuildConfig`, `BuildArtifact`, `BuildStep`, `SourceStep`, `Buildable`, `BuildDag`, plus the incremental-rebuild model.
- [Code Generation Steps](./codegen.md) — built-in steps that ship with Waveflow for generating C++ headers from Python schemas.
- [Python Simulation Pattern](./python.md) — recipe for writing a step that runs a SimPy simulation and produces in-memory or file artifacts.
- [Vitis Pattern](./vitis.md) — recipe for invoking Vitis HLS C-sim / C-synth and parsing the resulting reports.
- [Authoring `run.tcl`](./tcl.md) — the Vitis script shared by both single-kernel and composite flows.
- [XSI Build Rung](./xsi.md) — what XSI/`xvlog`/`xelab`/BFM mean and how the composite RTL rung runs end-to-end.
- [BFM Testbenches](./bfm.md) — the framework's AXI-MM / AXI-Stream models and their five-phase
  lifecycle, assembled from the testbench graph or by hand.
- [Sweeping a design](./sweep.md) — running the DAG at every point of a parameter grid to build a
  calibration corpus: `ParamGrid`, `Stage`, `SweepRunner` and `sweep_cli`.

## Quick example

A complete poly accelerator build, from schema to synthesis report, declared as one DAG:

```python
from waveflow.build.build import BuildConfig, BuildDag, SourceStep

dag = BuildDag()
dag.add(SourceStep(artifact="poly_source", path="poly.py"))

dag.add(BuildInputsStep(name="build_inputs"))                    # writes data/*.bin
dag.add(PySimStep(name="py_sim"))                                # writes results/sim/*
dag.add(ExtractPyTimingStep(name="extract_py_timing"))           # writes results/py_timing.json
dag.add(HlsGenIncludeStep(name="gen_include"))                   # writes include/*.h
dag.add(HlsCodegenStep(name="gen_kernel", comp_class=PolyAccel, ...))
dag.add(HlsCodegenStep(name="gen_tb",     comp_class=PolyTBHls, is_testbench=True, ...))
dag.add(CSimStep(name="csim"))                                   # invokes Vitis C-sim
dag.add(FunctionalVerifyStep(name="validate_csim", ...))         # py vs Vitis outputs
dag.add(CSynthStep(name="csynth"))                               # invokes Vitis C-synth + cosim
dag.add(InspectSynthStep(name="inspect_synth"))                  # parses csynth.xml
dag.add(ExtractCosimTimingStep(name="extract_cosim_timing", top="poly"))
dag.add(ValidateTimingStep(name="validate_timing", tolerance_cycles=20))

config = BuildConfig(root_dir=".", params={"nsamp": 100, "clk_freq": 100e6})
dag.run(config, through="extract_py_timing")          # stop before Vitis
# or:
dag.run(config)                                       # full build through validate_timing
```

See [examples/stream_inband/poly_build.py](https://github.com/sdrangan/waveflow/tree/main/examples/stream_inband/poly_build.py) for the full working pipeline this snippet is drawn from.
