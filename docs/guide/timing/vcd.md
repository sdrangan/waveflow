---
title: Extracting VCD Files
parent: Timing Analysis Tools
nav_order: 3
has_children: false
---

# Extracting VCD Files

The Vitis C/RTL simulation creates a `.wdb` (waveform database) file with traces of the simulated signals. This file is in an AMD proprietary format and cannot be read directly by most other tools, although you can inspect it in the Vivado viewer. Waveflow provides a helper that re-runs the existing RTL simulation and exports an open-source **VCD** or [**Value Change Dump**](https://en.wikipedia.org/wiki/Value_change_dump) file instead. VCD files can be read by many tools, including Python packages.

## Pre-Requisites

Prior to capturing the VCD file, you must first run the C/RTL co-simulation of the design.  To run co-simulation from a TCL file, use the command:

```tcl
cosim_design -trace_level [all | port]
```

It is important to set `trace_level` during co-simulation because the later VCD export reuses the trace selection recorded in the generated simulator Tcl.

## Parameters

Next, you will have to identify the following parameters.

| Parameter    | Default           | Description |
|--------------|-------------------|-------------|
| `top`        | *(required)*      | Top-level function name |
| `comp`       | `"hls_component"` | HLS component directory name |
| `out`        | `"dump.vcd"`      | Output filename (written to `vcd/<out>`) |
| `soln`       | `"solution1"`     | Solution subdirectory inside `comp` |
| `trace_level`| `"*"`             | VCD trace level: `"*"`/`"all"` for all signals, `"port"` for port signals only |
| `workdir`    | CWD               | Working directory containing the `comp` folder |


You can get the component and solution names from the directory structure created by Vitis. Running RTL co-simulation creates directories of the form `<comp>/<soln>`. For example, in the [polynomial example](../../examples/stream_inband/), the RTL simulation generates:

```bash
waveflow_poly_proj/solution1
```

So `comp` is `waveflow_poly_proj` and `soln` is `solution1`.


The value of `top` should match the top function used in the RTL simulation. In the polynomial example, the Tcl file contains `set_top poly`, so `top` is `poly`.


## Generating the VCD from the CLI

To generate the VCD file from the CLI, first activate the virtual environment where Waveflow is installed. Then run:

```bash
(env) xsim_vcd --top <top> [--comp <comp>] [--soln <soln>] [--out <out>] [--trace_level <level>]
```

For the polynomial example this would be:

```bash
(env) xsim_vcd --top poly --comp waveflow_poly_proj --soln solution1 --out dump_poly.vcd
```
If you want the smaller port-only trace, use:

```bash
(env) xsim_vcd --top poly --comp waveflow_poly_proj --soln solution1 --out dump_poly.vcd --trace_level port
```

After running the script, the VCD file for the example above will be written to:

```bash
poly/vcd/dump_poly.vcd
```

## Python API

You can also call the function from the `run_xsim_vcd` function that wraps the same logic as the CLI entry point:

```python
from waveflow.scripts.xsim_vcd import run_xsim_vcd
from pathlib import Path

vcd_path = run_xsim_vcd(
    top="poly",
    comp="waveflow_poly_proj",
    out="dump.vcd",
    workdir=Path("examples/stream_inband"),
)
print(f"VCD written to: {vcd_path}")
```


The `run_xsim_vcd` function returns a `pathlib.Path` pointing to the written VCD file. It raises the following errors:

- Raises `RuntimeError` on non-Windows platforms
- Raises `FileNotFoundError` if the simulation directory is not found
- Raises `RuntimeError` if xsim fails

## Understanding the `xsim_vcd.py` script

The `xsim_vcd.py` helper automates the VCD export by modifying the generated XSIM launch files and re-running the existing RTL simulation. In outline, it does the following:

1. After the original co-simulation has completed, it locates the simulator directory. For the histogram example this is typically one of:

```bash
histogram/waveflow_hist_proj/solution1/hls/sim/verilog
```

or

```bash
histogram/waveflow_hist_proj/solution1/sim/verilog
```

    This directory contains the generated RTL testbench, the Tcl batch file used by XSIM, and the original `run_xsim.bat` launcher.

2. It copies `<top>.tcl` to `<top>_vcd.tcl` and injects VCD commands before the existing `log_wave ...` command.

    For an all-signals trace, the inserted commands look like:

```tcl
open_vcd
log_vcd -r /
```

    If the original co-simulation used `trace_level port`, the generated Tcl already contains a filtered `log_wave [get_objects -filter ...]` command. In that case `xsim_vcd.py` reuses the same object selection for `log_vcd` instead of emitting the invalid command `log_vcd port`. This is why standalone commands such as `python hist_build.py --through generate_vcd --trace-level port` now produce a smaller port-only VCD successfully.

3. Near the end of the Tcl file, it changes:

```tcl
run all
quit
```

    to:

```tcl
run all
close_vcd
quit
```

4. In the same simulator directory, it copies `run_xsim.bat` to `run_xsim_vcd.bat` and changes the batch file so XSIM uses `<top>_vcd.tcl` instead of the original Tcl.

    The important line in the original batch file looks like:

```bash
call C:/Xilinx/2025.1/Vivado/bin/xsim ... -tclbatch hist.tcl -view hist_dataflow_ana.wcfg -protoinst hist.protoinst
```

    and the generated batch file contains:

```bash
cd /d "%~dp0"
call C:/Xilinx/2025.1/Vivado/bin/xsim ... -tclbatch hist_vcd.tcl -view hist_dataflow_ana.wcfg -protoinst hist.protoinst
```

    The added `cd /d "%~dp0"` makes the batch file runnable from any working directory.

5. Finally, the helper runs `run_xsim_vcd.bat`, waits for XSIM to finish, and copies the generated `dump.vcd` into the example's `vcd/` directory.

The rerun command is effectively:

```bash
run_xsim_vcd.bat
```

This re-runs the simulation and produces `dump.vcd`, which Waveflow then copies to the output path you requested.

## Practical notes

- If `--generate-vcd` fails, the most common cause is that the original co-simulation was run without trace capture enabled.
- Use `trace_level port` when you want a smaller VCD focused on top-level interface activity.
- Use `trace_level all` or `*` when you need internal RTL signals as well.
