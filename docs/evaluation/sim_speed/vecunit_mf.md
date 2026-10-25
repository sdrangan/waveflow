---
title: "Simulation speed: the matched filter on the vector unit"
parent: Simulation speed
nav_order: 5
summary: "One bit-exact matched-filter frame of 256 to 16,384 samples at 8 antennas (4 to 256 chunks on the vector unit) run four ways on the same operands: the application's untimed model (0.07 to 2.3 ms), the unit's interpreter (2.1 to 127 ms), Waveflow's pysim of the unit (8.9 to 280 ms) and the synthesized RTL under XSI (0.46 to 40 s, after 89 s of csynth and 85 s of XSI build). The RTL takes 52 to 144 times as long as the whole pysim. A streaming program moves a small block per event, so here the timing costs 1.6 to 2.2 times the interpreter's arithmetic, and the pysim over-charges a chunk by 11% (the calibration's known gap on this build). Measurement only: nothing has been optimized yet."
---

# Simulation speed: the matched filter on the vector unit

[The matched filter as a vector program](../../examples/vecunit_mf/index.md) run four ways on the same
operands, across frame lengths, on one build of [the vector unit](../../guide/dsp/vecunit.md). The
twin of [CG on the vector unit](vecunit_cg.md): the same unit and the same harness, with a streaming
program in place of an iterative one. This page measures; it does not optimize. Nothing in the pysim,
the interpreter or the harness was changed for it, and
[where the time goes](#where-the-pysims-time-goes) is recorded for the optimization that follows.

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
| 256×8 | 4 | 1,825 | 0.07 ms | 2.1 ms | **8.9 ms** (181 events) | **0.46 s** | **52×** |
| 1024×8 | 16 | 6,037 | 0.14 ms | 8.0 ms | **22 ms** (609 events) | **2.2 s** | **103×** |
| 4096×8 | 64 | 22,885 | 0.41 ms | 32 ms | **74 ms** (2,330 events) | **9.8 s** | **133×** |
| 16384×8 | 256 | 90,277 | 2.3 ms | 127 ms | **280 ms** (9,242 events) | **40 s** | **144×** |

The pysim column is the whole run, harness included. Apart:

| frame | pysim: build the harness | pysim: run | run ÷ interpreter | RTL ÷ run | pysim cycles (vs RTL) |
|---|---|---|---|---|---|
| 256×8 | 4.3 ms | 4.6 ms | 2.23 | 101× | 1,911 (+4.7%) |
| 1024×8 | 7.4 ms | 14 ms | 1.79 | 156× | 6,448 (+6.8%) |
| 4096×8 | 20 ms | 54 ms | 1.68 | 183× | 24,660 (+7.8%) |
| 16384×8 | 70 ms | 209 ms | 1.64 | 193× | 99,363 (+10.1%) |

Per 64-sample chunk, fitted over the four sizes (`setup + chunks × per chunk`): golden 0.009 ms,
interpreter 0.50 ms (16.5 ms element-major), pysim 1.08 ms (0.26 ms building the harness, 0.81 ms
running, 36 SimPy events), RTL 159 ms.

One-time costs, per design point: csynth 89 s; the XSI build 85 s the first time (compile 2.6,
elaborate 81, testbench 1.4), then nothing but the simulation for each new workload. The pysim needs
neither.

Measured 2026-10-24 (Vitis HLS / Vivado xsim 2024.1) on the machine of
[CG on the vector unit](vecunit_cg.md#results-across-sizes), otherwise idle; the untimed ways and the
pysim are the best of eleven runs, the RTL one run a size. A first sweep the same evening, from a build
root whose csynth was cached, gave the same cycles, and the interpreter, pysim and RTL times within 2%
(the golden's sub-millisecond times within 9%). The untimed and pysim columns are from a re-run after
Stage 9 of `plans/vecunit_new.md`, which fixed pysim's first-job start-up: the first frame's program no
longer delays it (6,483 cycles became 6,448 at 1024×8).

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

## Where the pysim's time goes

A profile of the largest pysim run (16384×8, 256 chunks, under cProfile, whose overhead inflates every
call: read the shares):

| | share |
|---|---|
| **building the harness** (28% of the pysim's time): the programs, the matrices into words | not split further |
| **the run** (72%) | |
| ... the interpreter (the execute task's passes and decode) | 53% |
| ... converting words to blocks and back (the load and store tasks) | 5.6% |
| ... SimPy, the interfaces, the tasks' own code | 41% |

The raw profile split is in `runtime.json` under `"profile"`.

## Insights

- **The RTL takes 52 to 144 times as long as the whole pysim,** and 101 to 193 times its run, before
  the RTL's three minutes of csynth and XSI build. The ratio grows with the frame: the pysim's fixed
  cost is a few milliseconds, and XSI's time grows with the cycles.
- **A streaming program pays more for its timing than CG does.** Each chunk is a small block (64
  samples) and a few instructions, so the events are many for the arithmetic: 36 a chunk against CG's
  117 a job, but a job's arithmetic is 27 times a chunk's. The pysim's run costs 1.6 to 2.2 times the
  interpreter's here, against 1.07 to 1.16 for CG, and SimPy with the framework is 41% of the run.
- **The pysim over-charges a chunk by 11%** (389 cycles against the RTL's 351), so its cycle count
  drifts from 4.7% over the RTL's for one frame of 4 chunks to 10.1% at 256. This is the calibration's
  known gap on this build, recorded by the calibration (Stage 7 of `plans/vecunit_new.md`): the timing
  was fitted on the CG build, whose execute
  decode loop is deeper (depth 23 against 14).
- **The interpreter costs 54 times the golden a chunk,** 0.50 ms against 0.009: the golden is a few
  numpy calls over the whole frame, the interpreter decodes and runs each chunk's 11 instructions. Its
  element-major mode costs another 33×, far more than for CG (4.5×), because the chunk's vectors are
  64 elements long, not 8.

## Future work

The optimizations this page points at, none done here:

- **SimPy and the framework**, 41% of the run: fewer events a chunk, or cheaper ones. For a streaming
  program this is the larger share.
- **The interpreter**, 53% of the run.
- **The timing**: take each build's decode-loop schedule into the per-pass and per-run costs (the
  calibration's recorded next step), which closes the 11% a chunk.
- **A host model that ends the RTL run** when the frame is done, so XSI simulates only the work's
  cycles and no scaling is needed.
