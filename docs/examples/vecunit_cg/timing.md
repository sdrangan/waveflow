---
title: CG on the vector unit, timing at RTL
parent: CG as a vector program
nav_order: 1
summary: "What the RTL measured (Vitis HLS / Vivado xsim 2024.1, xczu48dr, 4 ns) for CG on the vector unit, across workloads, word widths and lane counts: every job execute-bound, the loader and the store idle most of the time; 3,359 cycles an 8×32 solve of three iterations at L 4, 6,678 at L 2, 2,161 at L 8; 128-bit words change nothing. Where a job's time goes inside the execute task (42% decoding passes), what a trace shows, and how close pysim is (0.0 to 0.5% a job)."
---

# Timing at RTL

Every number on this page is an RTL run of the unit in its harness, bit-exact against pysim, from
`examples/vecunit_cg/cg_measure.py` (Vitis HLS / Vivado xsim 2024.1, `xczu48dr` at 4 ns, csynth's
estimated clock 3.09 to 3.11 ns on every build):

```bash
python -m examples.vecunit_cg.cg_measure --root /tmp/vu_cg_m     # -> measured.json, 13 runs, 4 builds
```

## The jobs against their bounds

A job's time is set by whichever of the unit's three tasks is slowest: the loader landing its input
blocks, the execute task running its program, or the store sending its outputs (consecutive jobs
overlap through the queues' two blocks). `waveflow.vecunit.report.job_bounds` gives each from the
program: the loader and the store at a word a cycle, the execute task from its calibrated timing.
Four solves a workload, each row the interval between consecutive statuses:

| Workload | Build | RTL, cycles a job | loader | store | execute | binds |
|---|---|---|---|---|---|---|
| `steady`: `8 × 32`, three iterations | `L 4`, 64-bit | **3,359** | 524 | 516 | 3,359 | execute |
| `steady_k2`: `k` 2 | `L 4`, 64-bit | **2,831** | 140 | 132 | 2,835 | execute |
| `steady_nit1`: one iteration | `L 4`, 64-bit | **1,399** | 262 | 258 | 1,396 | execute |
| `steady_nit8`: eight iterations | `L 4`, 64-bit | **8,259** | 1,179 | 1,161 | 8,265 | execute |
| `steady_n4`: one column group | `L 4`, 64-bit | **1,309** | 76 | 68 | 1,311 | execute |
| `steady` | `L 4`, 128-bit | **3,359** | 268 | 260 | 3,359 | execute |
| `steady_nit1` | `L 4`, 128-bit | **1,399** | 134 | 130 | 1,396 | execute |
| `steady` | `L 2`, 64-bit | **6,678** | 524 | 516 | 6,700 | execute |
| `steady` | `L 8`, 64-bit | **2,161** | 524 | 516 | 2,161 | execute |

**CG never waits for I/O on this unit.** The execute task takes 4 to 20 times the loader's time in
every workload, even with a single iteration, so 128-bit words change nothing: the same 3,359 cycles. The
cycle target that matters is the execute task's.

**The trace agrees.** `report.trace_report` reads a traced run (`steady`, 64-bit) by name: the loader's
`S` blocks wait for the execute task to free one (11 of 12 commits held), the store reads each block
5 cycles after the execute task commits it (it waits on every one after the first), the input port is busy 17% of the
run, and the execute task's firings run back to back, 3,358 cycles each.

## Where the execute task's time goes

The execute task's time, split by the calibrated timing (`VecTiming`, fitted at Stage 7):

| Workload | decoding passes | runs (fill, drain) | elements | scalar runs | control | total |
|---|---|---|---|---|---|---|
| `steady` | **1,396** (88 passes) | 727 (11 runs) | 698 | 519 (5 runs, 40 ops) | 18 | 3,358 |
| `steady_k2` | **1,396** | 727 | 175 | 519 | 18 | 2,835 |
| `steady_n4` | 174 (11 passes) | **661** (10 runs) | 87 | 370 | 18 | 1,310 |
| `steady`, `L 8` | 698 (44 passes) | 661 | 349 | 434 | 18 | 2,160 |

**Decoding is the largest share: 16 cycles a pass, 42% of a job.** csynth's schedule says why: the
decode loop (`RUNG`) runs at II 13 a pass, and delivering a pass's reductions (`DELIVER`) at II 3.
Shorter vectors cut only the elements -- `k` 2 saves 528 of 3,359 cycles -- and a solve with one
column group is mostly runs of a single pass, each paying the run's fill and drain. A decode stage that
runs ahead of execution (Stage 6 recorded it, not built) is where the next cycles are.

**Lanes.** Halving `L` doubles the column groups and so the passes: 6,678 cycles at `L 2`. Doubling it
gains less, 2,161 at `L 8` (1.55×), because the runs, the scalar runs and the control do not shrink
with the passes.

## Resources

csynth's estimate of the whole unit:

| Build | LUT | FF | DSP | BRAM18K |
|---|---|---|---|---|
| `L 4`, 64-bit | 82,063 | 71,661 | 53 | 36 |
| `L 4`, 128-bit | 81,653 | 71,631 | 53 | 36 |
| `L 2`, 64-bit | 53,400 | 35,399 | 29 | 20 |
| `L 8`, 64-bit | 133,449 | 126,131 | 101 | 72 |

## pysim against the RTL

pysim's job intervals are within **0.0 to 0.5%** of the RTL's on every workload and build here
(`measured.json`, `pysim_status`). Since Stage 9d a run's first job starts where the RTL's does too,
within 11 cycles on the `L 2` and `L 4` builds: the execute task starts a job when its program's first
word arrives -- the RTL decodes instructions as they stream in -- and keeps that lead through a queue
block it takes at once. Before, the first job of a long program started up to a program's length late
(350 cycles for eight iterations). On the `L 8` build the first job is still 120 cycles late, not yet
explained.

The [guide](../../guide/dsp/vecunit/timing.md#the-calibrated-models) has the timing's calibration, and the
fit by phase and configuration that Stage 9d tried and did not keep.
