---
title: CG on the vector unit, timing at RTL
parent: CG as a vector program
nav_order: 1
summary: "What the RTL measured (Vitis HLS / Vivado xsim 2025.1, xczu48dr, 4 ns) for CG on the vector unit, across workloads, word widths and lane counts: every job execute-bound, the loader and the store idle most of the time; 3,447 cycles an 8×32 solve of three iterations at L 4, 6,854 at L 2, 2,191 at L 8; 128-bit words change nothing. Where a job's time goes inside the execute task (44% decoding passes), what a trace shows, and how close pysim is (0.0 to 0.4% a job)."
---

# Timing at RTL

Every number on this page is an RTL run of the unit in its harness, bit-exact against pysim, from
`examples/vecunit_cg/cg_measure.py` (Vitis HLS / Vivado xsim 2025.1, `xczu48dr` at 4 ns, csynth's
estimated clock 3.09 to 3.11 ns on every build):

```bash
python -m examples.vecunit_cg.cg_measure --root /tmp/vu_cg_m     # -> measured.json, 15 runs, 4 builds
```

## The jobs against their bounds

A job's time is set by whichever of the unit's three tasks is slowest: the loader landing its input
blocks, the execute task running its program, or the store sending its outputs (consecutive jobs
overlap through the queues' two blocks). `waveflow.vecunit.report.job_bounds` gives each from the
program: the loader and the store at a word a cycle, the execute task from its calibrated timing
(filed at 2025.1).
Four solves a workload, each row the interval between consecutive statuses:

| Workload | Build | RTL, cycles a job | loader | store | execute | binds |
|---|---|---|---|---|---|---|
| `steady`: `8 × 32`, three iterations | `L 4`, 64-bit | **3,447** | 524 | 516 | 3,444 | execute |
| `steady_k2`: `k` 2 | `L 4`, 64-bit | **2,919** | 140 | 132 | 2,921 | execute |
| `steady_nit1`: one iteration | `L 4`, 64-bit | **1,439** | 262 | 258 | 1,435 | execute |
| `steady_nit8`: eight iterations | `L 4`, 64-bit | **8,467** | 1,179 | 1,161 | 8,466 | execute |
| `steady_n4`: one column group | `L 4`, 64-bit | **1,305** | 76 | 68 | 1,303 | execute |
| `steady` | `L 4`, 128-bit | **3,447** | 268 | 260 | 3,444 | execute |
| `steady_nit1` | `L 4`, 128-bit | **1,439** | 134 | 130 | 1,435 | execute |
| `steady` | `L 2`, 64-bit | **6,854** | 524 | 516 | 6,877 | execute |
| `steady` | `L 8`, 64-bit | **2,191** | 524 | 516 | 2,193 | execute |

**CG never waits for I/O on this unit.** The execute task takes 4 to 21 times the loader's time in
every workload, even with a single iteration, so 128-bit words change nothing: the same 3,447 cycles. The
cycle target that matters is the execute task's.

**The trace agrees.** `report.trace_report` reads a traced run (`steady`, 64-bit) by name: the loader's
`S` blocks wait for the execute task to free one (11 of 12 commits held), the store reads each block
5 cycles after the execute task commits it (it waits on every one after the first), the input port is busy 17% of the
run, and the execute task's firings run back to back, 3,446 cycles each.

## Where the execute task's time goes

The execute task's time, split by the calibrated timing (`VecTiming`, Stage 7's form fitted at 2025.1):

| Workload | decoding passes | runs (fill, drain) | elements | scalar runs | control | total |
|---|---|---|---|---|---|---|
| `steady` | **1,500** (88 passes) | 719 (11 runs) | 698 | 517 (5 runs, 40 ops) | 11 | 3,444 |
| `steady_k2` | **1,500** | 719 | 174 | 517 | 11 | 2,921 |
| `steady_n4` | 187 (11 passes) | **654** (10 runs) | 87 | 364 | 11 | 1,303 |
| `steady`, `L 8` | 750 (44 passes) | 654 | 349 | 430 | 11 | 2,193 |

**Decoding is the largest share: 17 cycles a pass, 44% of a job.** csynth's schedule says why: the
decode loop (`RUNG`) runs at II 14 a pass, and delivering a pass's reductions (`DELIVER`) at II 3.
Shorter vectors cut only the elements -- `k` 2 saves 528 of 3,447 cycles -- and a solve with one
column group is mostly runs of a single pass, each paying the run's fill and drain. A decode stage that
runs ahead of execution (Stage 6 recorded it, not built) is where the next cycles are.

**Lanes.** Halving `L` doubles the column groups and so the passes: 6,854 cycles at `L 2`. Doubling it
gains less, 2,191 at `L 8` (1.57×), because the runs, the scalar runs and the control do not shrink
with the passes.

## Resources

csynth's estimate of the whole unit (Vitis HLS 2025.1):

| Build | LUT | FF | DSP | BRAM18K |
|---|---|---|---|---|
| `L 4`, 64-bit | 82,960 | 61,310 | 53 | 40 |
| `L 4`, 128-bit | 82,870 | 61,243 | 53 | 40 |
| `L 2`, 64-bit | 55,009 | 30,493 | 29 | 24 |
| `L 8`, 64-bit | 131,778 | 107,612 | 101 | 44 |

## pysim against the RTL

pysim's job intervals, charged from the timing filed at 2025.1, are within **0.0 to 0.4%** of the
RTL's on every workload and build here (the held-out record in
`models/vecunit_timing/params.json`). Since Stage 9d a run's first job starts where the RTL's does too,
within 4 cycles on the `L 4` builds and 23 on `L 2`: the execute task starts a job when its program's
first word arrives -- the RTL decodes instructions as they stream in -- and keeps that lead through a
queue block it takes at once. Before, the first job of a long program started up to a program's length
late. On the `L 8` build the first job is still 131 cycles late, not yet explained.

The [guide](../../guide/dsp/vecunit/timing.md#the-calibrated-models) has the timing's calibration, and the
fit by phase and configuration that Stage 9d tried and did not keep.
