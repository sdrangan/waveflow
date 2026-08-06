---
title: C-Synth Resource Estimation
parent: Streaming polynomial
nav_order: 4
---

# C-synth resource estimation

The fourth group runs Vitis HLS C-synthesis on the generated kernel
and parses the report into a per-loop pipeline / II table plus a
total-resources summary.

| Step | Produces | What it does |
|------|----------|--------------|
| `csynth` | `report_dir` | Invokes `vitis_hls run.tcl` with `WAVEFLOW_POLY_COSIM=1`, which runs csim then csynth and (when cosim is enabled) RTL co-simulation; populates `waveflow_poly_proj/solution1/` |
| `inspect_synth` | `loop_df` | Parses `csynth.xml` via `waveflow.utils.csynthparse.CsynthParser`, prints the loop/resource tables, fails the build if any reported loop has `PipelineII > 1` |

## What gets reported

The `InspectSynthStep` walks the synthesis report (`csynth.xml`) for
every module in the solution and constructs two DataFrames:

- `loop_df` — one row per pipelined loop, columns:
  `PipelineII`, `PipelineDepth`, `TripCountMin`, `TripCountMax`,
  `LatencyMin`, `LatencyMax`.
- `res_df` — per-module + total + available resource counts (BRAM,
  DSP, FF, LUT, URAM).

Both tables are printed during the build; `loop_df` is also
serialized to `results/loop_df.csv` for downstream tooling.

A reported `PipelineII > 1` on any loop fails the build immediately
— II discipline is a property worth catching with a build-step rather
than buried in a synthesis log.

## Why it matters as a separate group

C-synthesis answers a different question from C-sim and from RTL
cosim: *can the kernel meet its target* and *what does it cost*?
Resource estimates are a first-class signal during exploration —
they're what you watch when sweeping `unroll_factor`, `in_bw`, or
`out_bw` looking for the Pareto front of throughput vs area.

The current `inspect_synth` step is the simplest useful consumer
of the csynth report.  A natural extension is a **parametric sweep**
step that drives `param_supports` variants through this group and
collects the resulting `(latency, II, BRAM, DSP, FF, LUT)` rows
into a single sweep table — see the kernel-variants plan for one
implementation sketch.

## Run just this group

```bash
python -m examples.stream_inband.poly_build --through inspect_synth
```

Produces `waveflow_poly_proj/solution1/syn/report/csynth.xml`,
`results/loop_df.csv`, and the inline resource / latency tables in
stdout.

---

Next: [RTL cosim timing verification →](./05_cosim_timing.md)
