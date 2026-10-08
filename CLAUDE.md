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
- **`waveflow/linalg/`** — Reusable complex fixed-point linear-algebra components:
  the systolic matrix multiply (`SystolicCore`, `SystolicUnit`) and the CG vector
  unit (`CgVectorCore`, `CgVectorUnit`). Each is a bit-exact Python model, a pysim
  module, a Vitis HLS task body in `waveflow/build/` (`systolic_*_task.h`,
  `cg_vector_*_task.h`, `wf_*.h`) and a cost model calibrated on the packaged
  platform `waveflow/calib/platforms/xczu48dr_250mhz_vitis2024_1/` (Vitis 2024.1).
  Tests in `tests/linalg/`; guide in `docs/guide/linalg/`.
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
- Run everything inside the repo's virtual environment (`source .venv/bin/activate`). With Vivado 2024.1 the XSI flow depends on it: activation exports `XILINX_VIVADO` and puts `.venv/xsi_compat/` on `LD_LIBRARY_PATH`. Without it every XSI run fails with "Failed to Load up XSI".
- A measured number belongs to a tool version. Vitis/Vivado 2024.1 does not reproduce every number recorded with 2025.1 (co-simulation of `examples/regmap` gives 49 cycles against 5), so name the version next to any measured number. The `examples/mimo_cg` study was measured entirely with 2024.1.
- The project is early-stage research software; many planned features are not yet built.
- Non-commercial use only under the Waveflow Research License.
