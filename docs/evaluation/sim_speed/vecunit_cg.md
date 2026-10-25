---
title: "Simulation speed: CG on the vector unit"
parent: Simulation speed
nav_order: 4
summary: "Bit-exact CG solves of an 8×n right-hand side (n = 32 to 2,048, 1 to 64 jobs on the vector unit) run four ways on the same operands: the application's untimed model (1 to 4 ms), the unit's interpreter (13 to 850 ms), Waveflow's pysim of the unit (81 ms to 5.0 s, cycle counts within 0.13% of the RTL's at 64 jobs) and the synthesized RTL under XSI (2.7 to 250 s, after 98 s of csynth and 93 s of XSI build). The RTL takes 34 to 50 times as long as the whole pysim and 180 to 280 times its run. Four-fifths of the pysim's time is building the harness, whose stimulus comes from a closed-loop run of the interpreter, and nine-tenths of its run is the interpreter. Measurement only: nothing has been optimized yet."
---

# Simulation speed: CG on the vector unit

[CG as a vector program](../../examples/vecunit_cg/index.md) run four ways on the same operands, across
problem sizes, on one build of [the vector unit](../../guide/dsp/vecunit.md). This page measures; it does
not optimize. Nothing in the pysim, the interpreter or the harness was changed for it, and
[where the time goes](#where-the-pysims-time-goes) is recorded for the optimization that follows.

## The set-up

The CG example, unchanged: `CgVecUnit` at the centre configuration (`Kmax 8, Nmax 32, L 4`, 12-bit
vectors, 20-bit `ps` and `rz`, 64-bit words), the build that Stages 5 to 7 of `plans/vecunit_new.md`
measured and calibrated. One problem per size: an `8 × 8` Hermitian positive definite `A` and an `8 × n` right-hand
side `B`, three iterations. A job holds at most `8 × 32`, and the columns are independent CG problems,
so a wider `B` runs as `n / 32` jobs, one column block each, back to back: this is how the problem grows
on fixed hardware. The harness plays the systolic array: each job's `S = A·P` matrices are in its
stimulus, computed by a closed-loop run of the interpreter. The same operands, and the same bits, for
all four:

```text
golden:       A, B --> waveflow.linalg.cg.cg_solve (the whole B at once)
interpreter:  programs, B, S = A·P --> waveflow.vecunit.model (run_cg, a job at a time)
pysim:        program, B and S streams --> CgVecUnit: load, execute, store tasks          (SimPy)
RTL:          AXI-Stream BFMs --> synthesized vecunit --> recorded outputs                 (XSI)
```

- **The golden** is the application's untimed model, what a system engineer would run: integer numpy
  over the whole `B`, every column at once.
- **The interpreter** is the unit's own bit-exact model, the three programs run instruction by
  instruction (`waveflow.vecunit.model`). Its passes run instruction-major (`"serial"`), which is what
  the pysim's execute task calls; the element-major mode (`"pass"`, the hardware's order, the
  interpreter's default) is recorded beside it.
- **The pysim** is `CgVecUnit` in its harness: building the harness (the stimulus, including its
  closed-loop interpreter run), writing the scenario, and the run. Its execute task calls the
  interpreter per instruction and charges the [calibrated timing](../../guide/dsp/vecunit.md#the-calibrated-models).
  Every output burst, every `P`, `X` and status, is checked against the interpreter's.
- **The RTL** is the csynth'd unit simulated by XSI through the system flow, untraced; its outputs are
  compared with the pysim's. csynth and the XSI build are reported apart from the simulation.

```bash
python -m examples.vecunit_cg.cg_runtime --rtl --root /tmp/vu_cg_rt   # -> runtime.json, "rhs_centre"
python -m examples.vecunit_cg.cg_runtime --profile                    # where the pysim's time goes
```

## Results across sizes

One solve per size; every run exact, in the interpreter (`X` against `cg_solve`), in pysim and in RTL:

| `B` | jobs | RTL cycles | golden | interpreter | pysim | RTL under XSI | RTL ÷ pysim |
|---|---|---|---|---|---|---|---|
| 8×32 | 1 | 3,491 | 0.98 ms | 12.9 ms | **81 ms** (143 events) | **2.7 s** | **34×** |
| 8×128 | 4 | 13,568 | 1.2 ms | 53 ms | **316 ms** (494 events) | **14 s** | **45×** |
| 8×512 | 16 | 53,876 | 1.8 ms | 213 ms | **1.27 s** (1,910 events) | **61 s** | **48×** |
| 8×2048 | 64 | 215,108 | 4.2 ms | 847 ms | **5.0 s** (7,574 events) | **250 s** | **50×** |

The pysim column is the whole run, harness included. Apart:

| `B` | pysim: build the harness | pysim: run | run ÷ interpreter | RTL ÷ run | pysim cycles (vs RTL) |
|---|---|---|---|---|---|
| 8×32 | 66 ms | 15 ms | 1.16 | 181× | 3,722 (+6.6%) |
| 8×128 | 258 ms | 58 ms | 1.09 | 245× | 13,801 (+1.7%) |
| 8×512 | 1,045 ms | 226 ms | 1.06 | 271× | 54,118 (+0.45%) |
| 8×2048 | 4,108 ms | 901 ms | 1.06 | 277× | 215,384 (+0.13%) |

Per job, fitted over the four sizes (`setup + jobs × per job`): golden 0.05 ms, interpreter 13.2 ms
(59.9 ms element-major), pysim 78 ms (64 ms building the harness, 14 ms running, 118 SimPy events), RTL
3.9 s.

One-time costs, per design point: csynth 98 s; the XSI build 93 s the first time (compile 2.8,
elaborate 89, testbench 1.4), then nothing but the simulation for each new workload. The pysim needs
neither.

Measured 2026-10-24 (Vitis HLS / Vivado xsim 2024.1) on a Linux workstation in the lab (Intel Core
i7-7700K, 4 cores and 8 threads at 4.2 GHz, 32 GB, Python 3.12.3), otherwise idle; the untimed ways and
the pysim are the best of eleven runs, the RTL one run a size.

**How the RTL column is counted.** As on [the systolic page](systolic.md#results-across-sizes): the
harness has no host to end the RTL run, so XSI simulates a fixed bound, `1.2 × pysim cycles + 2,000`,
and the table charges it `simulate × done ÷ bound`, where `done` is the cycle the last output word left
(for 8×2048, 303 s for 260,461 cycles, of which 215,108 were the work). That favours the RTL: the run's
fixed start-up is scaled down with it. XSI simulated this unit at 860 to 1,290 cycles a second. The raw
times are in `examples/vecunit_cg/runtime.json`.

## Where the pysim's time goes

A profile of the largest pysim run (8×2048, 64 jobs, under cProfile, whose overhead inflates every call:
read the shares):

| | share |
|---|---|
| **building the harness** (82% of the pysim's time) | |
| ... the closed-loop interpreter run that makes the stimulus | 93% |
| ... everything else: the problem, the programs, the matrices into words | 7% |
| **the run** (18%) | |
| ... the interpreter (the execute task's passes, scalars, decode) | 91% |
| ... converting words to blocks and back (the load and store tasks) | 1.2% |
| ... SimPy, the interfaces, the tasks' own code | 7.5% |

The raw profile split is in `runtime.json` under `"profile"`.

## Insights

- **The RTL takes 34 to 50 times as long as the whole pysim, and 180 to 280 times its run,** before
  the RTL's three minutes of csynth and XSI build. At 64 jobs that is four minutes against five seconds.
  A change to the unit or its configuration costs a new csynth and XSI build on the RTL side and
  nothing on the pysim side.
- **The pysim's timing is right where it was calibrated.** Its cycle count is within 0.13% of the
  RTL's at 64 jobs. After the first job, each job's interval is the RTL's to a cycle (at 8×128 the
  statuses leave 3,359, 3,360 and 3,360 cycles apart in pysim, 3,359 apart at RTL); the difference is
  the first job, whose status leaves 233 cycles late (3,594 against 3,361). The calibration (Stage 7
  of `plans/vecunit_new.md`) fitted the timing on the intervals between statuses, which never include a run's first job, so this start-up was never
  calibrated: 6.6% of a single job, 0.13% of 64.
- **Timing costs little on top of the arithmetic.** The pysim's run takes 1.06 to 1.16 times the
  interpreter's: about 118 SimPy events a job, 6 to 16% of the time. The cost model of
  [Simulation speed](index.md#the-cost-model) holds, and the untimed term is the interpreter.
- **The interpreter is the cost of programmability, in simulation.** It runs the unit's program
  instruction by instruction, 13 ms a job, where the golden does the whole `B` in numpy at 0.05 ms a job
  once its 1 ms of set-up is paid: 13 to 200 times the golden across these sizes. The element-major mode
  costs another 4.5×.
- **Most of the pysim's time is the testbench, not the simulation.** Building the harness is 82% of it,
  and 93% of that is the closed-loop interpreter run that computes each `S = A·P` from the `P` the unit
  emits, in the interpreter's slower element-major mode. In a system where the systolic engine is
  simulated beside the unit, `S` would come from that engine instead.

## Future work

The optimizations this page points at, none done here:

- **The stimulus.** Run the closed-loop capture in the interpreter's instruction-major mode (the
  same bits, 4.5× faster), or cache it per workload: most of the pysim's total.
- **The interpreter.** 91% of the run: its per-instruction work over the eight column groups and four
  lanes is where a faster pysim comes from.
- **The first job's start-up**, 233 cycles too long in pysim: calibrate the run's start as well as the
  intervals.
- **A host model that ends the RTL run** when the last job is done, so XSI simulates only the work's
  cycles and no scaling is needed (as for the systolic engine).
