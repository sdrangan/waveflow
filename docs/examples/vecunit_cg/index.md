---
title: CG as a vector program
parent: Examples
nav_order: 23
has_children: true
example_dir: examples/vecunit_cg
summary: "Conjugate gradient on A X = B (multi-RHS, complex fixed point) as a program for the programmable vector unit (VecUnit): the CG knowledge moves out of the hardware into a program builder, the unit stays application-free, and the solve is bit-exact with waveflow.linalg.cg's model in the interpreter, in pysim and at RTL. A harness plays the systolic array (S = A·P on each P the unit emits). What it shows: grouping the column groups' work kind by kind makes long fused runs, and the program runs a steady 8×32 solve of three iterations in 3,359 cycles against the dedicated CgVectorUnit's 3,581 (Vitis 2024.1), at about four times its LUTs."
---

# CG as a vector program

[The CG vector unit](../../guide/dsp/cg_vector.md) is the vector half of fixed-point conjugate
gradient built as hardware: the loop, the three passes per column group, the two guarded divides,
all in its HLS body. This example runs the same arithmetic as a **program** on
[the vector unit](../../guide/dsp/vecunit/index.md), which knows nothing about CG: the formats, the
register allocation and the order of the work all live in `examples/vecunit_cg/`.

## Learning objectives

1. Express an algorithm as **three programs** -- load, execute, store -- for a decoupled
   access/execute unit, with every application constant outside the hardware.
2. Map CG's registers onto **register groups and banks** so every pass keeps the pass rules.
3. Order the work so the unit **fuses** it: independent passes in one pipelined run.
4. Close a loop in a **static testbench**: the stimulus carries the `S` matrices a systolic multiply
   would return, taken from a closed-loop run of the interpreter.
5. Compare a program against a **dedicated kernel** in cycles and resources, at one tool version.

## The program

`cg_program.cg_config(formats)` builds a `VecUnitConfig` from a `CgFormats`: per column group `g` of
`L` columns, four vector groups `X_g, R_g, P_g, S_g` (groups `4g .. 4g + 3`, one per bank, so the
passes read four banks without conflict), five scalars `ps_g, alpha_g, beta_g, rz_g, rz2_g`; input
queues `B` and `S`, output queues `P` and `X`. `cg_programs(cfg, nit, k, n)` emits a job's three
programs. Per iteration and column group, the execute program is CG's steps 2-9:

```
wf.vlse.c      S_g, qS, g, G       # pass 1: S in, ps = q_ps(Σ Re(conj P · S))
wf.vdotr       ps_g, P_g, S_g
wf.sdivz       alpha_g, rz_g, ps_g # alpha (0 if ps == 0)
wf.vmacc.vs.c  X_g, alpha_g, P_g   # pass 2: X = q_X(X + P·alpha)
wf.vnmsac.vs.c R_g, alpha_g, S_g   #         R = q_R(R − S·alpha)
wf.vdotr       rz2_g, R_g, R_g     #         rz' = q_rz(Σ|R|²), on the new R
wf.sdivz       beta_g, rz2_g, rz_g # beta (0 if rz == 0)
wf.vmadd.vs.c  P_g, beta_g, R_g    # pass 3: P = q_P(P·beta + R)
wf.vsse        qP, P_g, g, G       #         (after the last iteration: X to qX)
```

with `wf.qpop qS` and `wf.qpush qP` around each iteration. In place of `rz = rz'` the builder swaps
the names `rz_g` and `rz2_g` each iteration: renaming in the program, not in hardware. The last
iteration skips the work only the next iteration would read (`skip_dead`). The load program is
`wf.ld qB` then one `wf.ld qS` per iteration; the store program one `wf.st` from `qP` per `P`, then
one from `qX`.

**Order.** The column groups are independent CG problems, so the default `order="kind"` emits every
group's pass 1, then every `alpha`, every pass 2, every `beta`, every pass 3: the execute task fuses
each kind into one run of eight passes, and each set of eight divides into one scalar run on the
pipelined dividers. `order="group"` (group by group, as above) gives the same bits in 46 pass runs and
40 scalar runs a job, against 11 and 5.

## The harness

`CgVecUnit` is a `VecUnit` whose scenarios are CG solves; it only supplies the configuration and the
jobs. A job's stimulus is its three programs, `B`, and the `nit` matrices `S`. Those come from the
interpreter run closed-loop: `waveflow.vecunit.model.run_job` calls back when the load program needs
an `S` that has not arrived, and the callback computes `S = cg.mm_step(A, P)` from the `P` the unit
just emitted -- the systolic array's part. The stream then carries exactly what the loop would.

| Workload | Jobs |
|---|---|
| `test` | four solves: `8 × 32` at three iterations, a short one (`k` 5, `n` 13: `L` does not divide `n`), one iteration, a zero column (both divide guards fire) |
| `steady` | four `8 × 32` solves of three iterations, the comparison with `CgVectorUnit` |
| `calib`, `holdout` | the timing calibration's pre-registered jobs (`waveflow/vecunit/calib.py`) |

## Running it

```bash
python -m examples.vecunit_cg.harness --root /tmp/vu_cg --through golden                   # pysim only
python -m examples.vecunit_cg.harness --root /tmp/vu_cg --through compare --workload steady # + csynth, XSI
python -m waveflow.vecunit.report /tmp/vu_cg
pytest tests/examples/test_vecunit_cg.py              # the program against cg.cg_solve, bit for bit
pytest -m xsi tests/vecunit/test_unit_xsi.py          # the RTL gates
python -m examples.vecunit_cg.cg_runtime --rtl --root /tmp/vu_cg_rt   # untimed vs pysim vs RTL, by size
```

What a solve costs to simulate, against the untimed models and the RTL, from one to 64 jobs, is
measured in [Evaluation: CG on the vector unit](../../evaluation/sim_speed/vecunit_cg.md); the RTL
across workloads, word widths and lanes, against each job's bounds, in [Timing at RTL](timing.md).

`tests/examples/test_vecunit_cg.py` runs the program on the interpreter against `cg.cg_solve` -- every
`P` and the final `X` -- for four format sets, `nit` 1 to 8, shapes at and below the maxima and the
zero guards, in pass and serial modes, both orders, with and without the dead work.

## What it measured

Vitis HLS / Vivado xsim 2024.1, `xczu48dr` at 4 ns, the `steady` workload, the formats `W12g8` (12-bit
vectors, 20-bit `ps` and `rz`, `g_div` 6) on both sides:

| | cycles a job | LUT | FF | DSP | BRAM |
|---|---|---|---|---|---|
| `CgVectorUnit` | 3,581 | 21,596 | 11,876 | 52 | 20 |
| vector unit, CG program | **3,359** (0.94x) | 82,063 | 71,661 | 53 | 36 |

The program is 6 % faster because it runs each kind of work across the eight column groups as one
pipelined run, and the eight divides of a kind side by side, where the dedicated core's loop order
serializes them. The price is resources: the fused execute loop, four pipelined dividers (one a
lane, about 33k FF together), and the run decode. A CG job is 233 instruction words: 88 passes in 11
runs, 704 run elements, 5 scalar runs of 8 divides, 9 control instructions; about 16 cycles of
decode a pass (42 % of the job, [Timing at RTL](timing.md)) is the largest share left, which a decode
stage running ahead of execution would hide.

**Re-measured with Vitis HLS / Vivado xsim 2025.1** (the same workload and formats, 2026-10-27;
`examples/vecunit_cg/measured_2025_1.json`):

| | cycles a job | LUT | FF | DSP |
|---|---|---|---|---|
| `CgVectorUnit` (rx, load, core, store) | 3,581 | 21,464 | 11,914 | 52 |
| vector unit, CG program | **3,447** (0.96x) | 82,624 | 60,668 | 53 |

`CgVectorUnit` takes the same cycles as at 2024.1; the vector unit about 3 % more (its decode loop runs
at II 14 against 13). The 2024.1 `CgVectorUnit` row above is the record of `plans/vecunit_new.md`
(Stages 5 and 6); the 2025.1 one comes from the bench of `tests/linalg/test_cg_vector_unit.py`.

pysim's timing is calibrated at RTL on CG-configuration jobs
([the guide](../../guide/dsp/vecunit/timing.md#the-calibrated-models)): held out, it predicts these steady
jobs within 0.02 % and fresh random jobs and solves within 0.64 %.
