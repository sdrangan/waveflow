# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install in development mode
pip install -e ".[dev]"

# Run all tests
pytest

# Run a specific test directory
pytest tests/hw/
pytest tests/simulation/
pytest tests/examples/

# Run a single test file
pytest tests/hw/test_dataschema.py

# Skip the slow toolchain tests (the usual dev loop — they need Vitis / Vivado installed)
pytest -m "not vitis and not xsi"

# Run only Vitis HLS integration tests (csynth / csim / cosim)
pytest -m vitis

# Run only the XSI RTL gates: the free-running kernels driven through real RTL by the BFM
# library, asserting exact cycle counts (mem_r_stream 158 / mem_w_stream 176 / mem_copy 2908; the
# interleaver_canon gate was retired in 2026-07).  Needs Vivado xsim + a C++
# compiler (the mingw g++ bundled with Vivado on Windows, the system g++ on Linux) AND a prior
# csynth of each top (they skip loudly if the RTL is absent).  The flow is driven by run.bat on
# Windows and run.sh on Linux -- see waveflow.build.trace_steps.xsi_runner_cmd and
# the xsi_tb_codegen plan (commit 3052952).
pytest -m xsi

# Lint / format
ruff check waveflow/
black waveflow/
mypy waveflow/
```

## Architecture

Waveflow is a Python-native hardware design platform. The philosophy is that Python is the **single source of truth** for hardware: simulation, synthesis, firmware, documentation, and AI tooling all derive from one Python specification.

### Core abstractions

**`DataSchema`** (`waveflow/hw/dataschema.py`) — The type system. A class-based schema where structure lives on the class and runtime values on the instance. Field subclasses: `IntField`, `FloatField`, `EnumField`, `DataList`, `DataArray`, `MemAddr`. This is the largest module (~3900 lines) and the foundation for code generation, firmware, and documentation.

**`HwModule`** (`waveflow/hw/hw_module.py`) — Base class for hardware objects (a `SimObj` with structure). Declares typed ports with direction (master/slave) using protocol types: FIFO, AXI-Stream, AXI-Lite, AXI-MM, and can contain sub-modules wired by internal interfaces. Following SystemC, one `HwModule` serves as either a leaf or a hierarchical top. Functional behavior is implemented as Python methods on slave ports or as a PyTorch `forward()` method. Subclasses `HostActivated` (host-launched) and `FreeRunMod` (free-running) map to the two realization flows.

**`Interface`** (`waveflow/hw/interface.py`) — Transactional connection between two hardware objects. Explicitly connects a master port on one `HwModule` to a slave port on another. Manages transactional semantics during simulation.

**`SimObj`** (`waveflow/simulation/simobj.py`) — Base class for anything participating in a simulation: hardware components, software processes, sensors, channels. Implements a three-phase lifecycle: `pre_sim()` → `run_proc()` → `post_sim()`.

**`Simulation`** (`waveflow/simulation/simulation.py`) — Runtime coordinator. Owns the SimPy discrete-event environment, drives the SimObj lifecycle, and connects interfaces between SimObjs.

### Subsystems

- **`waveflow/build/`** — Code generation for Vitis HLS (C++ API, stream utilities, TCL scripts).
- **`waveflow/toolchain/`** — Vitis HLS / Vivado toolchain detection and integration.
- **`waveflow/scripts/`** — CLI entry points (`sv_sim`, `sv_synth`, `sv_impl`, `waveflow_mcp_server`, etc.).
- **`waveflow/utils/`** — VCD waveform parsing, timing analysis, C-synthesis report parsing, fixed-point utilities.
- **`waveflow/mcp/`** — MCP server exposing hardware design tools to AI assistants (Claude Code, VS Code). Two modes: *workspace* (uses host file tools) and *headless* (self-contained, for CI/API use). `mcp/knowledge/` is local search and read over `docs/guide/` and the reference examples — an in-memory BM25 index built from the checkout at server start, also reachable as `waveflow kb <cmd>`.
- **`examples/`** — Reference designs. The curated set is the seventeen with a page under `docs/examples/<name>/index.md`, each naming its directory in an `example_dir:` front-matter key; the rest are older work. `waveflow kb examples` lists them.

### Simulation flow

1. Instantiate `Component` subclasses and `Interface` objects wiring their ports.
2. Create a `Simulation`, pass in the components and interfaces.
3. `Simulation.run()` calls `pre_sim()` on all SimObjs, then schedules their `run_proc()` coroutines inside SimPy, then calls `post_sim()` for teardown/analysis.

### Designing an accelerator

Start from `docs/guide/patterns/` (*Design patterns*): command-response is the default shape (a command
carrying `n` and a `tx_id`, the work on `n` elements, a response that echoes it; through a pipeline the
command travels with the data and the last stage answers) -- the page links every layer it touches.

### Writing HLS kernel bodies

Before writing or reviewing a kernel body, read `docs/guide/vectorization/hls/loop_optimization.md`
(*Design patterns for loop optimization*): the lane loop for several samples per bus word (a word per
iteration with the compute unrolled, or one element per iteration reading a word every PF), a
straight-line loop per message as the default body shape (a single-firing state machine is an
optimization), and the timing rule "do not decide, compute and commit in one iteration".  Never pack or
unpack a word by hand -- use the generated `<elem>_array_utils` lane routines.

### Synthesis flow

A Component's Python behavior is translated to Vitis HLS C++ via `BuildConfig` (`build/build.py`). `sv_synth` / `sv_impl` scripts drive Vitis and Vivado from generated TCL. AI-assisted prompt generation can derive HLS code from the Python `forward()` specification.

## Notes

- Python 3.10+ required.
- Vitis HLS is optional and only needed for synthesis tests (`-m vitis`). The toolchain is auto-detected by `waveflow/toolchain/toolchain.py`.
- The project is early-stage research software; many planned features are not yet built.
- Non-commercial use only under the Waveflow Research License.
