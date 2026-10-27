---
title: "Simulation speed: the matched filter on the vector unit"
parent: Simulation speed
nav_order: 5
summary: "One bit-exact matched-filter frame of 256 to 16,384 samples at 8 antennas (4 to 256 chunks on the vector unit) run four ways on the same operands: the application's untimed model (0.07 to 2.3 ms), the unit's interpreter (1.5 to 89 ms), Waveflow's pysim of the unit (8.3 to 239 ms) and the synthesized RTL under XSI (0.46 to 40 s, after 89 s of csynth and 85 s of XSI build). The RTL takes 56 to 169 times as long as the whole pysim. A streaming program moves a small block per event, so its run is half SimPy and the framework; making the unit's own Python faster without changing a result (Stage 10) sped its interpreter 1.4 times and its pysim 1.1 to 1.2 times. pysim over-charges a chunk by 11%, the calibration's known gap on this build."
---

# Simulation speed: the matched filter on the vector unit

[The matched filter as a vector program](../../examples/vecunit_mf/index.md) run four ways on the same
operands, across frame lengths, on one build of [the vector unit](../../guide/dsp/vecunit/index.md). The
twin of [CG on the vector unit](vecunit_cg.md): the same unit and the same harness, with a streaming
program in place of an iterative one. Stage 8 of `plans/vecunit_new.md` measured it; Stage 10 then made
the unit's own Python faster without changing any result. The tables are the current code's;
[Faster, with no result changed](#faster-with-no-result-changed) has the before and after.

## The set-up

The matched-filter example, unchanged: `MfVecUnit` as built (`VLMAX 64, nrx_max 8, L 4`, 16-bit
formats, 64-bit words), the build that Stages 5 to 7 of `plans/vecunit_new.md` measured. One frame per
size, `Y[i, j] = conj(X[i]) · R[i, j]` for `nsamp` samples and 8 antennas, strip-mined into chunks of 64
samples: one job, its programs `5 + 3·2 = 11` execute instructions a chunk. The same operands, and the
same bits, for all four:

```text
golden:       X, R --> mf_golden (the whole frame, numpy)
interpreter:  programs, X, R --> waveflow.vecunit.model (run_mf)
pysim:        program, X and R streams --> MfVecUnit: load, execute, store tasks      (SimPy)
RTL:          AXI-Stream BFMs --> synthesized vecunit --> recorded outputs             (XSI)
```

- **The golden** is the application's untimed model: `fixputils` on stored integers over the whole
  frame, a few numpy calls.
- **The interpreter** is the unit's own bit-exact model, the programs run instruction by instruction.
  Its passes run instruction-major (`"serial"`), what the pysim's execute task calls; the element-major
  mode (`"pass"`, the hardware's order and the interpreter's default) is recorded beside it.
- **The pysim** is `MfVecUnit` in its harness: building the harness (the program and data streams),
  writing the scenario, and the run. Every output burst, each chunk's `Y` and the status, is checked
  against the interpreter's.
- **The RTL** is the csynth'd unit simulated by XSI through the system flow, untraced; its outputs are
  compared with the pysim's. csynth and the XSI build are reported apart from the simulation.

```bash
python -m examples.vecunit_mf.mf_runtime --rtl --root /tmp/vu_mf_rt   # -> runtime.json, "frame_vlmax64_nrx8"
python -m examples.vecunit_mf.mf_runtime --profile                    # where the pysim's time goes
```

## Results across sizes

One frame per size; every run exact, in the interpreter (`Y` against the golden), in pysim and in RTL:

| frame | chunks | RTL cycles | golden | interpreter | pysim | RTL under XSI | RTL ÷ pysim |
|---|---|---|---|---|---|---|---|
| 256×8 | 4 | 1,825 | 0.07 ms | 1.5 ms | **8.3 ms** (173 events) | **0.46 s** | **56×** |
| 1024×8 | 16 | 6,037 | 0.14 ms | 5.6 ms | **19 ms** (578 events) | **2.2 s** | **116×** |
| 4096×8 | 64 | 22,885 | 0.40 ms | 22 ms | **63 ms** (2,205 events) | **9.8 s** | **155×** |
| 16384×8 | 256 | 90,277 | 2.3 ms | 89 ms | **239 ms** (8,733 events) | **40 s** | **169×** |

The pysim column is the whole run, harness included. Apart:

| frame | pysim: build the harness | pysim: run | run ÷ interpreter | RTL ÷ run | pysim cycles (vs RTL) |
|---|---|---|---|---|---|
| 256×8 | 4.1 ms | 4.1 ms | 2.81 | 112× | 1,911 (+4.7%) |
| 1024×8 | 6.7 ms | 12.5 ms | 2.24 | 179× | 6,448 (+6.8%) |
| 4096×8 | 17 ms | 46 ms | 2.07 | 213× | 24,660 (+7.8%) |
| 16384×8 | 58 ms | 180 ms | 2.03 | 224× | 99,363 (+10.1%) |

Per 64-sample chunk, fitted over the four sizes (`setup + chunks × per chunk`): golden 0.009 ms,
interpreter 0.35 ms (10.6 ms element-major), pysim 0.92 ms (0.22 ms building the harness, 0.70 ms
running, 34 SimPy events), RTL 159 ms.

One-time costs, per design point: csynth 89 s; the XSI build 85 s the first time (compile 2.6,
elaborate 81, testbench 1.4), then nothing but the simulation for each new workload. The pysim needs
neither.

Measured 2026-10-24 (Vitis HLS / Vivado xsim 2024.1) on the machine of
[CG on the vector unit](vecunit_cg.md#results-across-sizes), otherwise idle; the untimed ways and the
pysim are the best of eleven runs, the RTL one run a size. A first sweep the same evening, from a build
root whose csynth was cached, gave the same cycles, and the interpreter, pysim and RTL times within 2%
(the golden's sub-millisecond times within 9%). Stage 9 of `plans/vecunit_new.md` fixed pysim's
first-job start-up: the first frame's program no longer delays it (6,483 cycles became 6,448 at
1024×8). `runtime.json` keeps the record from before Stage 10 under
`frame_vlmax64_nrx8_before_stage10`.

**How the RTL column is counted.** As for CG: XSI simulates a fixed bound, `1.2 × pysim cycles +
2,000`, and the table charges it `simulate × done ÷ bound`, where `done` is the cycle the last output
word left (for 16384×8, 54.5 s for 121,835 cycles, of which 90,277 were the frame). XSI simulated this
build at 2,240 to 3,940 cycles a second, two to three times as fast as the CG build (csynth's estimate:
74.5k LUT and 48.3k FF here, 82.1k and 71.7k there). The raw times are in
`examples/vecunit_mf/runtime.json`.

**The input floor.** A chunk moves 288 input words (`X`, 32; `R`, 256), so the frame's floor is
`4.5·nsamp` words. The RTL takes 1.58 times it for one frame of 4 chunks, falling to **1.22** at 256
chunks (351 cycles a chunk, the [steady figure](../../examples/vecunit_mf/index.md#the-input-floor)
of 1,408 cycles a 4-chunk frame).

## Faster, with no result changed

Stage 10 of `plans/vecunit_new.md` made the unit's own Python faster (`waveflow/vecunit/` and the
examples only) and changed no result: every output burst of 17 pysim workloads carries the same words
at the same simulated time as before; [CG's page](vecunit_cg.md#faster-with-no-result-changed) lists
the changes. Same machine, idle, best of eleven:

| frame | interpreter | pysim: build | pysim: run | pysim | SimPy events |
|---|---|---|---|---|---|
| 256×8 | 2.05 → 1.47 ms (1.39×) | 4.3 → 4.1 ms (1.05×) | 4.6 → 4.1 ms (1.11×) | 8.9 → 8.3 ms (1.08×) | 181 → 173 |
| 1024×8 | 8.0 → 5.6 ms (1.44×) | 7.4 → 6.7 ms (1.10×) | 14.4 → 12.5 ms (1.15×) | 21.8 → 19.3 ms (1.13×) | 609 → 578 |
| 4096×8 | 31.8 → 22.2 ms (1.43×) | 20.0 → 17.1 ms (1.17×) | 53.5 → 46.1 ms (1.16×) | 73.6 → 63.2 ms (1.16×) | 2,330 → 2,205 |
| 16384×8 | 127 → 89 ms (1.43×) | 70 → 58 ms (1.20×) | 209 → 180 ms (1.16×) | 280 → 239 ms (1.17×) | 9,242 → 8,733 |

The interpreter gains as CG's does; the pysim gains much less, because the matched filter's harness
runs no interpreter (no closed loop) and half its run is SimPy and the framework, outside the unit.

## Where the pysim's time goes

A profile of the largest pysim run (16384×8, 256 chunks, under cProfile, whose overhead inflates every
call: read the shares), after Stage 10 (before, in brackets):

| | share |
|---|---|
| **building the harness**: the programs, the matrices into words | 28% (28%) |
| **the run** | 72% (72%) |
| ... the interpreter (the execute task's passes and decode) | 47% (53%) |
| ... converting words to blocks and back (the load and store tasks) | 6.5% (5.6%) |
| ... SimPy, the interfaces, the tasks' own code | 47% (41%) |

The raw profile split is in `runtime.json` under `"profile"`.

## Insights

- **The RTL takes 56 to 169 times as long as the whole pysim,** and 112 to 224 times its run, before
  the RTL's three minutes of csynth and XSI build. The ratio grows with the frame: the pysim's fixed
  cost is a few milliseconds, and XSI's time grows with the cycles.
- **A streaming program pays more for its timing than CG does.** Each chunk is a small block (64
  samples) and a few instructions, so the events are many for the arithmetic: 34 a chunk against CG's
  104 a job, but a job's arithmetic is 21 times a chunk's. The pysim's run costs 2.0 to 2.8 times the
  interpreter's here, against 1.00 to 1.36 for CG, and SimPy with the framework is 47% of the run.
- **The pysim over-charges a chunk by 11%** (389 cycles against the RTL's 351), so its cycle count
  drifts from 4.7% over the RTL's for one frame of 4 chunks to 10.1% at 256. This is the calibration's
  known gap on this build, recorded by the calibration (Stage 7 of `plans/vecunit_new.md`): the timing
  was fitted on the CG build, whose execute
  decode loop is deeper (depth 23 against 14).
- **The interpreter costs 39 times the golden a chunk,** 0.35 ms against 0.009: the golden is a few
  numpy calls over the whole frame, the interpreter decodes and runs each chunk's 11 instructions. Its
  element-major mode costs another 31×, far more than for CG (4.7×), because the chunk's vectors are
  64 elements long, not 8.

## Future work

- **SimPy and the framework**, 47% of the run: fewer events a chunk, or cheaper ones -- the stream
  transfers and the stream-of-blocks handshakes, outside the unit's code. For a streaming program
  this is now the larger share.
- **The interpreter**, 47% of the run.
- **The timing**: take each build's decode-loop schedule into the per-pass and per-run costs (the
  calibration's recorded next step), which closes the 11% a chunk.
- **A host model that ends the RTL run** when the frame is done, so XSI simulates only the work's
  cycles and no scaling is needed.
