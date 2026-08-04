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

# Run only the XSI RTL gates: the four free-running kernels driven through real RTL by the BFM
# library, asserting exact cycle counts (158 / 176 / 2835 / 3469).  Needs Vivado xsim + mingw g++
# AND a prior csynth of each top (they skip loudly if the RTL is absent).  See
# plans/xsi_tb_codegen.md.
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
- **`waveflow/mcp/`** — MCP server exposing hardware design tools to AI assistants (Claude Code, VS Code). Two modes: *workspace* (uses host file tools) and *headless* (self-contained, for CI/API use). RAG over a pre-built example corpus lives in `mcp/corpus/`.
- **`examples/`** — Reference designs: `poly/` (polynomial), `conv2d/`, `histogram/`, `interface/`, `timing/`.

### Simulation flow

1. Instantiate `Component` subclasses and `Interface` objects wiring their ports.
2. Create a `Simulation`, pass in the components and interfaces.
3. `Simulation.run()` calls `pre_sim()` on all SimObjs, then schedules their `run_proc()` coroutines inside SimPy, then calls `post_sim()` for teardown/analysis.

### Synthesis flow

A Component's Python behavior is translated to Vitis HLS C++ via `BuildConfig` (`build/build.py`). `sv_synth` / `sv_impl` scripts drive Vitis and Vivado from generated TCL. AI-assisted prompt generation can derive HLS code from the Python `forward()` specification.

## Notes

- Python 3.10+ required.
- Vitis HLS is optional and only needed for synthesis tests (`-m vitis`). The toolchain is auto-detected by `waveflow/toolchain/toolchain.py`.
- The project is early-stage research software; many planned features are not yet built.
- Non-commercial use only under the Waveflow Research License.
