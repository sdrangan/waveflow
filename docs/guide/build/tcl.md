---
title: Authoring run.tcl
parent: Build System
nav_order: 5
summary: "What is inside the run.tcl the Vitis steps invoke, which the pattern page treats as a black box. The core sequence from open_project to csim_design, csynth_design and the optional cosim_design, shared by single-kernel and composite projects alike."
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
open_project -reset $proj                     ;# w32_proj or w64_proj
set_top $top                                  ;# poly or poly_bw64
add_files gen/poly.cpp -cflags "-I."
add_files -tb poly_tb.cpp -cflags "-I. -DPOLY_WORD_BW=$width"
open_solution -reset "solution1"
create_clock -period $clk_period_ns
csim_design -argv "$data_dir csim"
csynth_design
cosim_design -argv "$data_dir cosim timing" -trace_level $trace_level
```

## The stage switch used by build steps

The example's build steps set environment variables before launching Vitis, because
`vitis-run` 2025.1 has no `--tclargs`:

- `CSimStep` sets `WAVEFLOW_POLY_STAGE=csim`;
- `CSynthStep` sets `WAVEFLOW_POLY_STAGE=synth`;
- both set `WAVEFLOW_POLY_WIDTH` (32 or 64), which picks the top, the project and the data.

In TCL, that becomes a branch around the design commands:

```tcl
set stage $::env(WAVEFLOW_POLY_STAGE)
if {$stage eq "csim"} {
    csim_design -argv "$data_dir csim"
    exit 0
}
csynth_design
cosim_design -argv "$data_dir cosim timing" -trace_level $trace_level
```

This makes one `run.tcl` usable for both "compile/sim-only" and "full RTL cosim" runs, at every
width.

## Practical authoring checklist

- Keep project/solution names stable (`open_project`, `open_solution`) so build steps can locate outputs predictably.
- Add both generated kernel code and generated/hand-authored testbench files via `add_files` and `add_files -tb`.
- Keep `set_top` aligned with the generated top function.
- Prefer env-driven switches (stage, width, clock period) over hard-coding run variants in multiple scripts.
- Keep each project one directory deep: Vitis HLS 2025.1 drops the kernel from csim in a nested project.

## See also

- [Vitis Pattern](./vitis.md) — build-step wrappers that call `run.tcl`.
- [XSI Build Rung](./xsi.md) — when the final RTL rung is driven outside Vitis cosim.
