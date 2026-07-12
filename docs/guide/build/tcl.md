---
title: Authoring run.tcl
parent: Build System
nav_order: 5
---

# Authoring `run.tcl`

The Vitis pattern page shows **how** a build step invokes Vitis (`vitis_hls run.tcl`), but often treats the TCL script itself as a black box. This page fills that gap.

`run.tcl` is the control script for the Vitis rungs in the build ladder. It is shared by both single-kernel and composite projects whenever those projects run through Vitis (`csim`, `csynth`, optional `cosim`).

## Core commands

Most Waveflow examples use this core sequence:

1. `open_project -reset ...`
2. `add_files ...` for kernel sources
3. `add_files -tb ...` for testbench sources
4. `set_top ...`
5. `open_solution -reset ...`
6. `create_clock -period ...`
7. `csim_design`
8. `csynth_design`
9. `cosim_design` (only when enabled)

From [`examples/stream_inband/run.tcl`](https://github.com/sdrangan/waveflow/tree/main/examples/stream_inband/run.tcl):

```tcl
open_project -reset waveflow_poly_proj
set_top poly
add_files gen/poly.cpp -cflags "-I."
add_files -tb gen/poly_tb.cpp -cflags "-I."
open_solution -reset "solution1"
create_clock -period $clk_period_ns
csim_design -argv "$data_dir"
csynth_design
cosim_design -argv "$data_dir" -trace_level $trace_level
```

## The `COSIM` branch used by build steps

Waveflow build steps toggle environment variables before launching Vitis:

- `CSimStep` sets `..._COSIM=0`
- `CSynthStep` sets `..._COSIM=1`

In TCL, that becomes a branch around `cosim_design`:

```tcl
set do_cosim 0
if {[info exists ::env(WAVEFLOW_POLY_COSIM)]} {
    set do_cosim [expr {$::env(WAVEFLOW_POLY_COSIM) in {1 true TRUE yes YES}}]
}

csim_design -argv "$data_dir"
csynth_design
if {$do_cosim} {
    cosim_design -argv "$data_dir" -trace_level $trace_level
}
```

This makes one `run.tcl` usable for both "compile/sim-only" and "full RTL cosim" runs.

## Practical authoring checklist

- Keep project/solution names stable (`open_project`, `open_solution`) so build steps can locate outputs predictably.
- Add both generated kernel code and generated/hand-authored testbench files via `add_files` and `add_files -tb`.
- Keep `set_top` aligned with the generated top function.
- Prefer env-driven switches (`COSIM`, clock period, trace level) over hard-coding run variants in multiple scripts.

## See also

- [Vitis Pattern](./vitis.md) — build-step wrappers that call `run.tcl`.
- [XSI Build Rung](./xsi.md) — when the final RTL rung is driven outside Vitis cosim.
