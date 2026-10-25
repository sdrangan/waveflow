---
title: "Simulation speed: CG on the vector unit"
parent: Simulation speed
nav_order: 4
summary: "Bit-exact CG solves of an 8×n right-hand side (n = 32 to 2,048, 1 to 64 jobs on the vector unit) run four ways on the same operands: the application's untimed model (1 to 4 ms), the unit's interpreter (8.5 to 474 ms), Waveflow's pysim of the unit (27 ms to 1.0 s, cycle counts within 0.07% of the RTL's) and the synthesized RTL under XSI (2.7 to 250 s, after 98 s of csynth and 93 s of XSI build). The RTL takes 101 to 250 times as long as the whole pysim. Making the unit's own Python faster without changing a single result (Stage 10 of the plan) cut the pysim by 3 to 5 times and the interpreter by 1.6 to 1.8 times: how, and what is left."
---

# Simulation speed: CG on the vector unit

[CG as a vector program](../../examples/vecunit_cg/index.md) run four ways on the same operands, across
problem sizes, on one build of [the vector unit](../../guide/dsp/vecunit.md). Stage 8 of
`plans/vecunit_new.md` measured it; Stage 10 then made the unit's own Python faster without changing
any result. The tables are the current code's;
[Faster, with no result changed](#faster-with-no-result-changed) has the before and after.

## The set-up

The CG example, unchanged: `CgVecUnit` at the centre configuration (`Kmax 8, Nmax 32, L 4`, 12-bit
vectors, 20-bit `ps` and `rz`, 64-bit words), the build that Stages 5 to 7 of `plans/vecunit_new.md`
measured and calibrated. One problem per size: an `8 × 8` Hermitian positive definite `A` and an
`8 × n` right-hand side `B`, three iterations. A job holds at most `8 × 32`, and the columns are
independent CG problems, so a wider `B` runs as `n / 32` jobs, one column block each, back to back:
this is how the problem grows on fixed hardware. The harness plays the systolic array: each job's
`S = A·P` matrices are in its stimulus, computed by a closed-loop run of the interpreter. The same
operands, and the same bits, for all four:

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
  interpreter per instruction and charges the
  [calibrated timing](../../guide/dsp/vecunit.md#the-calibrated-models). Every output burst, every `P`,
  `X` and status, is checked against the interpreter's.
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
| 8×32 | 1 | 3,491 | 1.0 ms | 8.5 ms | **27 ms** (129 events) | **2.7 s** | **101×** |
| 8×128 | 4 | 13,568 | 1.2 ms | 31 ms | **73 ms** (438 events) | **14 s** | **195×** |
| 8×512 | 16 | 53,876 | 1.8 ms | 120 ms | **277 ms** (1,686 events) | **61 s** | **221×** |
| 8×2048 | 64 | 215,108 | 4.2 ms | 474 ms | **1.00 s** (6,678 events) | **250 s** | **250×** |

The pysim column is the whole run, harness included. Apart:

| `B` | pysim: build the harness | pysim: run | run ÷ interpreter | RTL ÷ run | pysim cycles (vs RTL) |
|---|---|---|---|---|---|
| 8×32 | 15 ms | 12 ms | 1.36 | 234× | 3,489 (−0.07%) |
| 8×128 | 39 ms | 34 ms | 1.10 | 419× | 13,568 (0.00%) |
| 8×512 | 154 ms | 122 ms | 1.02 | 501× | 53,885 (+0.02%) |
| 8×2048 | 525 ms | 475 ms | 1.00 | 526× | 215,151 (+0.02%) |

Per job, fitted over the four sizes (`setup + jobs × per job`): golden 0.05 ms, interpreter 7.4 ms
(35 ms element-major), pysim 15 ms (8.1 ms building the harness, 7.4 ms running, 104 SimPy events),
RTL 3.9 s.

One-time costs, per design point: csynth 98 s; the XSI build 93 s the first time (compile 2.8,
elaborate 89, testbench 1.4), then nothing but the simulation for each new workload. The pysim needs
neither.

Measured 2026-10-24 (Vitis HLS / Vivado xsim 2024.1) on a Linux workstation in the lab (Intel Core
i7-7700K, 4 cores and 8 threads at 4.2 GHz, 32 GB, Python 3.12.3), otherwise idle; the untimed ways and
the pysim are the best of eleven runs, the RTL one run a size. `runtime.json` keeps the record from
before Stage 10 under `rhs_centre_before_stage10`.

**How the RTL column is counted.** As on [the systolic page](systolic.md#results-across-sizes): the
harness has no host to end the RTL run, so XSI simulates a fixed bound, `1.2 × pysim cycles + 2,000`,
and the table charges it `simulate × done ÷ bound`, where `done` is the cycle the last output word left
(for 8×2048, 303 s for 260,461 cycles, of which 215,108 were the work). That favours the RTL: the run's
fixed start-up is scaled down with it. XSI simulated this unit at 860 to 1,290 cycles a second. The raw
times are in `examples/vecunit_cg/runtime.json`.

## Faster, with no result changed

Stage 10 of `plans/vecunit_new.md` made the unit's own Python faster -- `waveflow/vecunit/` and the
example's code only; nothing in `fixputils`, `lanes`, `cg.cg_solve`, the interfaces or SimPy -- and
changed no result: every output burst of 17 pysim workloads (both applications, the generic unit, four
builds; 724 bursts) carries the same words at the same simulated time as before, and the RTL is
untouched. Same machine, idle, best of eleven:

| `B` | interpreter | pysim: build | pysim: run | pysim | SimPy events |
|---|---|---|---|---|---|
| 8×32 | 13.2 → 8.5 ms (1.55×) | 67 → 15 ms (4.4×) | 15 → 12 ms (1.33×) | 82 → 27 ms (**3.1×**) | 141 → 129 |
| 8×128 | 53 → 31 ms (1.72×) | 257 → 39 ms (6.7×) | 58 → 34 ms (1.72×) | 316 → 73 ms (**4.3×**) | 489 → 438 |
| 8×512 | 213 → 120 ms (1.78×) | 1,041 → 154 ms (6.7×) | 230 → 122 ms (1.88×) | 1.27 s → 277 ms (**4.6×**) | 1,893 → 1,686 |
| 8×2048 | 853 → 474 ms (1.80×) | 4,070 → 525 ms (7.8×) | 909 → 475 ms (1.91×) | 4.98 → 1.00 s (**5.0×**) | 7,509 → 6,678 |

What changed, each found by profiling the runs above:

- **The interpreter's arithmetic.** It is `fixputils`' integer arithmetic on small arrays (four lanes
  by eight elements), where each call's generality cost more than the arithmetic: deriving and hashing
  a result format (a dataclass of enums), the width guard, dtype choices, numpy's `clip` wrapper. The
  interpreter now runs the same integer steps for its signed operands with each format pair's plan
  derived once, and quantizes a value's real and imaginary parts in one call. Fuzzed against
  `fixputils`, bit for bit.
- **A program checked once.** The interpreter checked every instruction and pass of every job, and the
  assembler formatted a disassembly for every pass it checked -- for a message only an error prints. A
  job's checks and its assembly are now cached across the jobs of one solve, one harness or one golden
  (never across runs, so a timing of independent runs stays cold), and the messages are formatted only
  for an error.
- **The stimulus.** The harness builds each `S` by running the interpreter closed-loop -- before, in its
  element-major mode, 4.5× slower for the same bits -- and the golden then ran the interpreter again.
  Now the closed loop runs instruction-major, and its results are the golden.
- **The execute task's work, once a program.** It decoded and gathered every job's program again, and
  waited after every item; it now decodes a program once, and waits only where another task can see
  its clock (a queue block taken, freed or committed, its status written): the same times, 11% fewer
  SimPy events.

## Where the pysim's time goes

A profile of the largest pysim run (8×2048, 64 jobs, under cProfile, whose overhead inflates every
call: read the shares), after Stage 10 (before, in brackets):

| | share |
|---|---|
| **building the harness** | 50% (82%) |
| ... the closed-loop interpreter run that makes the stimulus | 89% (93%) |
| **the run** | 50% (18%) |
| ... the interpreter (the execute task's passes, scalars, decode) | 77% (90%) |
| ... converting words to blocks and back (the load and store tasks) | 2.7% (1.3%) |
| ... SimPy, the interfaces, the tasks' own code | 21% (8.4%) |

The raw profile splits are in `runtime.json` under `"profile"`.

## Insights

- **The RTL takes 101 to 250 times as long as the whole pysim, and 234 to 526 times its run,** before
  the RTL's three minutes of csynth and XSI build. At 64 jobs that is four minutes against one second.
  A change to the unit or its configuration costs a new csynth and XSI build on the RTL side and
  nothing on the pysim side.
- **The pysim's timing is right where it was calibrated.** Its cycle count is within 0.07% of the
  RTL's at every size, and exactly the RTL's at four jobs. The first measurement found the first job
  233 cycles late -- the execute task waited for its whole program burst, 233 words, where the RTL
  decodes each instruction as it arrives -- 6.6% of a single job. The calibration (Stage 7 of
  `plans/vecunit_new.md`) fitted the intervals between statuses, which never include a run's first
  job; Stage 9 starts a job at its program's first word, and the gap closed.
- **Timing costs almost nothing on top of the arithmetic.** The pysim's run takes 1.00 to 1.36 times
  the interpreter's time on the same jobs: about 104 SimPy events a job. The cost model of
  [Simulation speed](index.md#the-cost-model) holds, and the untimed term is the interpreter.
- **The interpreter is the cost of programmability, in simulation.** It runs the unit's program
  instruction by instruction, 7.4 ms a job, where the golden does the whole `B` in numpy at 0.05 ms a
  job once its 1 ms of set-up is paid: 8 to 112 times the golden across these sizes. Its element-major
  mode costs another 4.7×.
- **Half the pysim's time is still the testbench.** The stimulus is a closed-loop interpreter run; in a
  system where the systolic engine is simulated beside the unit, `S` would come from that engine instead.

## Future work

- **The interpreter**, 77% of the run and most of the stimulus: its per-instruction work over the
  eight column groups is where a faster pysim still comes from -- running a pass over all its column
  groups at once, for one.
- **A host model that ends the RTL run** when the last job is done, so XSI simulates only the work's
  cycles and no scaling is needed (as for the systolic engine).
