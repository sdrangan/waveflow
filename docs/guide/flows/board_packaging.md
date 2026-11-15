---
title: Board packaging development
parent: Modules
nav_order: 7
audience: python
snippets: run
summary: "Develop the template-driven RFSoC4x2 board packaging extension: Board and calibration Platform responsibilities, receipt-bound HLS export and control ABI, reusable DAG steps, Tcl topology contracts, S1/S2 entry points, portable PYNQ hosts, and the checks required when adding a board or accelerator."
---

# Board packaging development

This guide describes the implemented, template-driven path from one exported HLS
accelerator to an RFSoC4x2 overlay and a portable PYNQ host. It is the developer
reference for `waveflow.board`, the board-related `waveflow.build` modules, and
the S1/S2 examples.

The [device/system packaging flow](../build/ipi.md) also uses the existing build
infrastructure, with IP boundaries and connections derived from `SystemTopSpec`.
The application path here has its own committed BD template and `BdDesign`
contracts, including the processing system and its connections.

## Scope and current acceptance

| Layer | Implementation |
| --- | --- |
| Board target | RealDigital RFSoC4x2, `xczu48dr-ffvg1517-2-e`, board files revision `1.0` |
| Tools | Vitis HLS and Vivado `2025.1`, checked by the runners |
| S1 | SimpFun controlled by PS AXI-Lite, with one 100 MHz PS PL clock |
| S2 | Current upstream PolyAccel with PS DDR, simple AXI DMA and 32-bit AXIS; the same 100 MHz clock |
| Software checks | Board configuration, export/ABI/driver checks, model/host protocols and generated sources have offline regression gates |
| Fresh tool/board acceptance | Run manually for the migrated implementation; historical board results are not its acceptance evidence |

The board examples provide buildable implementation paths. A successful Python
simulation, BD validation or overlay collection establishes its own validation
level. Physical acceptance requires running the portable host on RFSoC4x2 with
the newly collected overlay. Later board stages remain future application work.

## Module boundaries

| Location | Responsibility |
| --- | --- |
| [`waveflow/board/spec.py`](../../../waveflow/board/spec.py) | Immutable `Board`, `ClockProfile`, `ClockSource` and the bundled RFSoC4x2 target |
| [`waveflow/board/clock.py`](../../../waveflow/board/clock.py) | Explicit `ClockRequest` / `ClockResolution` and Vivado nominal clock readback |
| `waveflow/board/boards/` and `waveflow/board/tcl/` | Board-file resources and clock/discovery recipes shipped with the Python package |
| [`waveflow/build/ip_export.py`](../../../waveflow/build/ip_export.py) | Fresh board-targeted HLS synthesis receipts, export, IP identity and package integrity |
| [`waveflow/build/hls_control.py`](../../../waveflow/build/hls_control.py) | Parse and cross-check Vitis `control.h` and IP-XACT against register intent |
| [`waveflow/build/host_driver.py`](../../../waveflow/build/host_driver.py) | Generate the standalone register driver and its ABI-bound manifest |
| [`waveflow/build/vivado_board.py`](../../../waveflow/build/vivado_board.py) | `BdDesign` contracts, isolated Vivado projects, validation, implementation and collection |
| [`waveflow/build/board_steps.py`](../../../waveflow/build/board_steps.py) | Optional `BuildStep` adapters for the existing `BuildDag` |
| `waveflow/toolchain/` | Tool discovery and process invocation, including `run_vivado` |
| `examples/*/board/` | Application-owned topology, host protocol, model adapter and manual notebook |
| `tests/board/` | Core conformance fixtures, failure cases and opt-in AMD-tool gates |

Board packaging is an optional software capability of Waveflow. Application
arithmetic, DMA framing and board topology stay in their respective application
modules. The extension reuses `BuildConfig`, `BuildDag`, kernel code generation
and the shared `run_dag_cli` interface.

### Board and calibration Platform

`Board` supplies the physical device, board preset, tool release and clock
profiles. `Platform` identifies the measured calibration library for a part and
HLS target clock. Constructing a platform identity does not produce calibration
measurements.

```python
from pathlib import Path
from waveflow.board import Board
from waveflow.build.build import BuildConfig

board = Board.rfsoc4x2()
config = BuildConfig(
    root_dir=Path("board_build/my_accel"),
    board=board,
    clock_profile="ps_control",
    vitis_version="2025.1",
    platform="rfsoc4x2_ps_control_100mhz",  # Optional calibration identity.
    platforms_root=Path("board_build/platforms"),
)
assert config.part == "xczu48dr-ffvg1517-2-e"
assert config.clk_freq == 100e6
```

`BuildConfig` derives and checks the board target before resolving the calibration
platform. It rejects conflicting explicit part/clock values and a mismatched
calibration platform, including when `allow_platform_mismatch=True`. Existing
builds without a board retain the upstream platform/default target behavior.

A `ClockProfile` distinguishes physical frequency from the HLS scheduling target.
For example, the declared `rf_axis` profile is 245.76 MHz physical and 250 MHz HLS;
S1/S2 use `ps_control`, which is 100 MHz for both. `board.clock()` is a lookup.
`board.resolve_clock(ClockRequest(...))` explicitly invokes Vivado; constructing
`BuildConfig` never does. A resolved clock can be supplied as `resolved_clock`
instead of `clock_profile`. It must be accepted and compatible with the board.

## Build stages and artifact contracts

The application supplies source/code-generation steps and a synthesis step using
`synthesize_for_board()`. It then appends the reusable steps below. Each step
publishes its result only after its checks succeed.

| Application step | Shared step / producer | Main artifact |
| --- | --- | --- |
| `synthesize` | Application step calling `synthesize_for_board` and checking C/RTL results | `hls_synthesis_receipt` |
| `export_ip` | `ExportIpStep` | `ip_manifest`, `ip_directory`, `ip_archive` |
| `control_abi` | `HlsControlSpecStep` | `hls_control_spec` object, `hls_control_manifest` path |
| `host_driver` | `HostDriverGenStep` | `host_driver`, `host_driver_manifest` |
| `export_host_sim` | Application check using the freshly generated driver | `export_host_sim_result` |
| `bd_gen` | `BdGenStep` | `bd_inputs` |
| `bd_validate` | `BdValidateStep` | `bd_validation` |
| `bitstream` | `BitstreamStep` | `bitstream_manifest` |
| `overlay` | `CollectOverlayStep` | `overlay_manifest` |

The synthesis/export receipts bind the target, input snapshot, RTL, reports and
packaged files with content hashes. The ABI binds the driver and BD to the same
export. Later stages recheck these inputs before publishing their receipts.

The core functions use unique run directories. Their path/object artifacts are
in-memory DAG values, so use `--force` for a complete fresh CLI run, or an explicit
force selection when calling `dag.run()` for standalone steps. A later process
can continue from explicit receipt paths using `SourceStep`; the notebooks use
this to implement a validated design without repeating HLS/export.

### Control ABI and generated host

`HlsControlSpecStep` requires the actual exported header/XML and a `VitisRegMap`
expressing design intent. It checks names, widths, offsets, access, control side
effects and supported array/pointer regions. It follows the kernel generator's
C++ enum representation (`ap_uint<8>`), independently of compact stream enum
serialization. Explicit `pointer_bindings` associate a generated pointer register
with its AXI master; DDR wiring and allocation are application responsibilities.

`HostDriverGenStep` generates `driver.py`, `control_abi.json` and
`driver_manifest.json`. The generated runtime uses only Python's standard
library. Its backend supplies `read_registers(offset, count)` and
`write_registers(offset, values)` over unsigned 32-bit words. Scalars preserve
the verified signed/float representation; array access uses raw ABI element values.

The examples' model-only register adapters are used before export. After export,
`export_host_sim` runs the actual exported-ABI driver against the Python model.
Model-only layouts are not published as verified hardware ABIs.

## Tcl template and topology contracts

Each application has two reviewed inputs:

- `board/bd/template.tcl` creates cells and connects their pins/interfaces.
- `board/design.py` returns a `BdDesign` with expected addresses, connections,
  properties, clock source and clock pin.

The framework creates the Vivado project and BD, registers the board/IP
repositories, sources the template, assigns the declared addresses, and checks
the resulting design. The template receives:

| Variable | Meaning |
| --- | --- |
| `WF_BD` | Block-design name |
| `WF_VLNV` | Verified exported IP identity |
| `WF_IP_CELL` | Application-selected accelerator instance name |
| `WF_REQUEST_HZ` | Physical clock request |

A template creates/connects its PS, reset, interconnect, DMA and accelerator cells.
It must leave address assignment to the framework and must not launch synthesis.
Use `BdAddress` for byte apertures, `BdConnection` for interface/net membership and
`BdProperty` for required cell/pin settings. See the complete
[S1 design contract](../../../examples/regmap/board/design.py) and
[S2 contract](../../../examples/stream_inband/board/design.py).

`bd_validate` invokes Vivado's `validate_bd_design` and then checks exact
connection membership, required properties, address windows and nominal clock
readback. It does not synthesize or implement the design. The framework does not
automatically infer a board topology from arbitrary IP interfaces; mismatched
bus/pin names, widths or topology must be resolved in the template and contracts.

`bitstream` builds a fresh project from validated inputs, repeats BD checks, and
requires passing routed setup/hold, severe-DRC and unconstrained-timing checks.
It checks the HWH address aperture and the BIT embedded in the XSA before
publishing the implementation receipt.

### When a template can be reused

A new IP can reuse a template when its board-facing interface contract remains
compatible: interface names/protocols, stream widths, clock/reset connections and
required control/memory apertures. Regenerate the IP export, ABI and driver for
every changed IP; the AXI-Lite base address and the IP's internal register offsets
are separate concerns.

Changing a polynomial degree can preserve the S2 BD when AXIS remains 32 bits and
the control/clock/reset interfaces remain compatible. Update the Python schemas,
model, required C++ hook/body, serializers, host vectors and tests together. A
changed header size must flow into DMA lengths. Adding an AXI memory master,
another stream or another clock domain requires corresponding topology work.

## S1 and S2 application structure

| Area | S1 | S2 |
| --- | --- | --- |
| Canonical Python source | [`simp_fun.py`](../../../examples/regmap/simp_fun.py) | [`poly.py`](../../../examples/stream_inband/poly.py) |
| C++ implementation | `simp_fun_compute_impl.cpp` | `poly_body_impl.tpp` |
| Board entry | [`simp_fun_board.py`](../../../examples/regmap/simp_fun_board.py) | [`poly_board.py`](../../../examples/stream_inband/poly_board.py) |
| Template and contracts | `examples/regmap/board/` | `examples/stream_inband/board/` |
| Portable host | `host/run_simp_fun.py` | `host/run_poly.py`, `host/dma.py` |
| Manual notebook | [`build_simp_fun.ipynb`](../../../examples/regmap/board/build_simp_fun.ipynb) | [`build_poly.ipynb`](../../../examples/stream_inband/board/build_poly.ipynb) |

The current S2 coefficients arrive in each DATA header, and AXI-Lite exposes only
`halted`, `error` and `tx_id` status. DATA and END headers have six 32-bit words.
The host arms S2MM before each output-producing transfer and sends DATA, samples
and END through MM2S. The response echoes the transaction ID; successful final
status is zero. The portable host rejects the old register-coefficient protocol.

S2 uses separate SmartConnects for control and DDR access. DMA MM2S/S2MM access
PS DDR through HP0 in the lower 2 GiB aperture. The accelerator, DMA, interconnect
and reset use the same 100 MHz PS PL clock. New buffering or clock-domain crossings
must be designed explicitly if an extension needs them.

Python owns the schemas and reference behavior. The current S2 generator emits
the kernel boundary around its handwritten C++ body and uses the upstream
handwritten C++ testbench/scenarios. Follow that component's code-generation
contract when adding arithmetic; changing Python reference behavior alone does
not update a handwritten C++ body.

## Adding another accelerator or board

1. Define the accelerator's schemas, register intent and model; implement its
   required synthesis hook/body and establish model/C/RTL checks.
2. Add an application board entry reusing the existing source and code-generation
   steps. Have board synthesis produce a fresh receipt for the selected target.
3. Set `IpExportSpec` identity and required interfaces, then append export, ABI and
   driver steps. Exercise the generated driver with the application's host model.
4. Reuse or write a Tcl template and declare all relevant `BdDesign` contracts.
   Run `bd_validate` before implementation.
5. Append implementation/collection steps. Supply the portable host's buffer,
   framing, cache maintenance and timeout rules for the board data path.
6. Add offline failure/conformance tests, explicit AMD-tool gates and a fresh
   physical-board acceptance procedure.

For a new board, add its `Board` configuration and versioned board resources,
select or extend the supported clock recipe, and test target/clock consistency.
Keep vendor licenses and ensure the resources ship in both wheel and source
packages. A board definition supplies target information; the application still
provides its PS/peripheral topology and interface contracts.

## Manual development commands

For a fresh checkout, create the development environment once:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

Use the intended checkout's Python environment. From the checkout root, set the
installed AMD tool paths (adjust the installation location when needed):

```bash
source .venv/bin/activate
unset PYTHONPATH
export WAVEFLOW_VITIS_PATH="$HOME/Xilinx/2025.1/Vitis/bin/vitis-run"
export WAVEFLOW_VIVADO_PATH="$HOME/Xilinx/2025.1/Vivado/bin/vivado"
export MPLCONFIGDIR=/tmp/waveflow-mpl
```

Inspect the available steps without launching a build:

```bash
python -m examples.regmap.simp_fun_board --list-steps
python -m examples.stream_inband.poly_board --list-steps
```

Build each overlay independently when ready:

```bash
python -m examples.regmap.simp_fun_board \
  --platform rfsoc4x2_ps_control_100mhz \
  --platforms-root examples/regmap/board_build/platforms \
  --through overlay --force --timing

python -m examples.stream_inband.poly_board \
  --platform rfsoc4x2_ps_control_100mhz \
  --platforms-root examples/stream_inband/board_build/platforms \
  --through overlay --force --timing
```

Use `--through host_sim` for software checks or `--through bd_validate` to stop
before implementation. The CLI also provides `--list-artifacts`, `--status` and
`--force-step`. The manual notebooks have separate BD/implementation switches,
both initially `False`, and finish with full environment/CMD instructions.

The CMD output root is `examples/<app>/board_build/`; each notebook creates a
fresh run under that root's `notebook/` directory. Collected overlays appear under
`overlays/overlay-*/manifest.json` relative to the selected build root. S2 also
writes `vectors/cubic.json`, `identity.json` and `constant.json`.

### Tests and physical acceptance

Run the core offline gates without AMD tools or a board:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest \
  tests/board tests/examples/test_board_packaging.py \
  tests/examples/test_board_dma.py tests/examples/test_board_build_notebooks.py \
  tests/build/test_build_config_platform.py tests/build/test_tcl_target.py \
  tests/toolchain/test_toolchain.py \
  -m 'not vitis and not vivado and not board and not xsi and not gem5'
```

With the tool environment above, explicitly run the fresh S1/S2 HLS/export/ABI/BD
gates when ready:

```bash
python -m pytest tests/examples/test_board_packaging_tools.py -v
```

A collected overlay contains BIT/HWH/XSA, `driver.py`, `control_abi.json`,
`driver_manifest.json`, reports and a relocatable rebuild bundle. Its manifest
records hashes, target, addresses and implementation verdicts, and keeps
`board_execution_verified=False`. The ABI/driver/rebuild evidence is why the
build output includes more than a BIT/HWH pair. The upstream legacy
`collect_overlay` script retains its own interface; this flow uses
`CollectOverlayStep`.

For physical acceptance, copy the entire collected overlay directory and the
application's portable host to a fresh board test directory. Copy the matching
S2 vectors as well. Run the portable host with `--backend pynq` in the board's
PYNQ-enabled environment, and retain new acceptance JSON files. The host verifies
the collected package before programming and stages a matching BIT/HWH pair
without a sibling XSA for PYNQ metadata loading. Choose new output paths on reruns.

Use the exact manifest returned by the successful build for each test. Do not mix
an old overlay or driver with new vectors, and do not use historical physical
results to mark a changed implementation accepted.
