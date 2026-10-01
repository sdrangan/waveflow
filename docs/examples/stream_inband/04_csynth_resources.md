---
title: C-Synth Resource Estimation
parent: Streaming polynomial
nav_order: 4
summary: "Running C-synthesis and parsing the report into a per-loop pipeline and initiation-interval table plus a resource summary. The step fails the build when any reported loop has an initiation interval above 1, so a pipelining regression stops the build rather than being noticed later."
---

# C-synth resource estimation

The fourth group runs Vitis HLS C-synthesis on the kernel (the generated boundary
around the hand-written body) and parses the report into a per-loop pipeline / II table
plus a total-resources summary.

| Step | Produces | What it does |
|------|----------|--------------|
| `csynth` | `report_dir` | Runs `run.tcl` with `WAVEFLOW_POLY_STAGE=synth`: C synthesis, then RTL co-simulation of the timing scenario (see the next page); populates `waveflow_poly_proj/solution1/` |
| `inspect_synth` | `loop_df`, `res_df` | Parses `csynth.xml` via `waveflow.utils.csynthparse.CsynthParser`, prints the loop and resource tables, and fails the build if any reported loop has `PipelineII > 1` |

## What gets reported

The `InspectSynthStep` walks the synthesis report (`csynth.xml`) for
every module in the solution and constructs two DataFrames:

- `loop_df` — one row per pipelined loop, columns:
  `PipelineII`, `PipelineDepth`, `TripCountMin`, `TripCountMax`,
  `LatencyMin`, `LatencyMax`.
- `res_df` — per-module + total + available resource counts (BRAM,
  DSP, FF, LUT, URAM).

Both tables are printed during the build and written to `results/loop_df.csv` and
`results/res_df.csv`; the final `summary` step collects them into `results/summary.json`.
The sample loop pipelines at II = 1.

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
python examples/stream_inband/poly_build.py --through inspect_synth
```

Produces `waveflow_poly_proj/solution1/syn/report/csynth.xml`,
`results/loop_df.csv`, `results/res_df.csv`, and the inline resource / latency tables in
stdout.

---

Next: [RTL cosim timing verification →](./05_cosim_timing.md)
