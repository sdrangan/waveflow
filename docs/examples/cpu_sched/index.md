---
title: Micro-scheduler on a CPU
parent: Examples
nav_order: 20
example_dir: examples/cpu_sched
summary: "Software on a calibrated processor, beside hardware: a micro-scheduler runs on the A53 platform model, keeps a ready list with the very algorithm the platform was calibrated on, and dispatches seeded Poisson jobs to SimpFun accelerators over AXI-Lite with interrupt completion. The report prices every scheduler operation -- latency, queueing, energy, footprint, confidence -- and the scheduler's trace is replayed on gem5 to check the model."
---

# Micro-scheduler on a CPU

The first user of the [processor model](../../guide/cpu/index.md): a scheduler that is *software*,
running on the calibrated Cortex-A53, handing jobs to accelerators that are *hardware*.

```bash
python -m examples.cpu_sched.cpu_sched --jobs 200 --accels 2
```

## Learning objectives

What this adds to the examples before it -- software on a processor -- in the order the page teaches it:

1. Write software as `SwFunction` calls on a calibrated `Processor`, each call run on the real data and
   priced from the work counters it reports by the platform's cycle and energy models.
2. Drive host-activated accelerators over AXI-Lite from software, sleeping on each one's interrupt while
   the core is free.
3. Make each decision inside the call that commits it, so two processes waiting for a core cannot act on
   the same state, and check that every job is dispatched exactly once.
4. Read the report: latency, queueing, energy, footprint, and how far each price is from the
   calibration (`INTERPOLATED`, `EXTRAPOLATED`).
5. Record the run as a trace, replay it on gem5, and state the model's error against it.

## The system

- **Jobs** arrive as a seeded Poisson stream (0.6 µs mean inter-arrival), each with a priority.
  Each arrival's `add` is a call of its own, so a busy core delays the add, never the next arrival.
- **The scheduler is software on the CPU.** Its ready list is a Python list sorted by
  `(priority, id)`, and every change to it is a call on the `Processor`: an arrival is an `add`, a
  dispatch removes the head (`delete`) and hands it over (`dispatch`), and a periodic aging pass
  promotes the least urgent waiting job (`reprio`). Each call runs the calibrated `sched_ops`
  algorithm on the *real* list and is priced by the platform's models from the counters it reports.
- **The accelerators are hardware.** Two [`SimpFun`](../regmap/index.md) kernels behind their own
  AXI-Lite links: a dispatcher writes `x`, `a`, `b`, launches, sleeps on the interrupt and reads `y`.
  The bus time is the bus model's; the CPU core is free meanwhile.

## The report

```text
200 jobs in 209.7 us simulated; all correct: True
CPU utilization [0.793]; energy 26.329 uJ (dynamic 6.009, static 20.320)
  sched_ops.add      n  200  mean   1731.2 ns  wait  1335.5 ns    12994.8 pJ/call  EXTRAPOLATED {'EXTRAPOLATED': 26, 'INTERPOLATED': 174}  ws 736.0 l1  code 696 B
  sched_ops.delete   n  200  mean    687.5 ns  wait   401.3 ns    12944.2 pJ/call  INTERPOLATED {'INTERPOLATED': 200}  ws 744.0 l1  code 696 B
  dispatch           n  200  mean    561.3 ns  wait   461.0 ns     1917.7 pJ/call  INTERPOLATED {'INTERPOLATED': 200}  ws None None  code 112 B
  sched_ops.reprio   n   13  mean   3824.6 ns  wait  3066.7 ns    33704.8 pJ/call  INTERPOLATED {'INTERPOLATED': 13}  ws 744.0 l1  code 696 B
```

`sched_ops.add` reads `EXTRAPOLATED` for exactly the 26 arrivals that found the list empty: the
model was fitted on lists of 1 to 1,024 entries. The list peaks at 92 jobs. Static power dominates
the energy although the core is busy four fifths of the time. At 0.3 µs the system cannot keep up:
the list grows to 177 jobs and the run takes 319 µs. (These are the Vitis 2025.1 platform's models;
an `add` priced about 70 cycles dearer than at 2024.1 is most of the difference from the figures
first recorded here: 48 jobs, 158 µs.)

## Three races it found

Building the example exposed the same mistake twice. The dispatcher first read the head of the list,
*then* called `delete`; but a call's body runs when it is granted a core, and with two dispatchers
waiting for the core both read the same head. One job ran twice and another was stranded, and the
run never ended. The aging pass made the same mistake with its target. Both choices now happen
inside the call, and `run()` raises unless every job is dispatched exactly once — the rule is on the
[scheduling page](../../guide/cpu/scheduling.md).

A code review found a third, quieter one. The arrival process made each `add` call itself, so it
drew the next arrival only once the previous add had finished: the stream configured at 0.3 µs
arrived every 0.6 µs, and the report looked healthy. Each add now runs in a process of its own.

## Checking the model against gem5

The scheduler's operations are recorded as a trace (`--trace`), and
[`sched_replay.c`](../../../examples/cpu_sched/sched_replay.c) replays them on gem5's HPI core with
the same list algorithm (`python -m examples.cpu_sched.replay`). The recorded trace,
`tests/fixtures/cpu/cpu_sched_trace.jsonl`, comes from an earlier run of the example: 410
operations and 200 dispatches. The replay sees exactly the trace's counters. In one measured region
around all of them, gem5 measured 63,124
cycles and the model predicted 90,347: **43.1 % high**. The model was calibrated one isolated call at
a time, and a scheduler running back to back pays less per call. The compiler widened the gap: with
Vitis 2025.1's gcc 13.3.0 an `add` measured alone costs about 70 cycles more than with 2024.1's gcc
12.2.0, while the sequence's total barely moved (63,266 cycles then, a 19.6 % miss). That is a
documented limit of the shipped platform; see [Calibration](../../guide/cpu/calibration.md).

Pricing the same trace through the model takes about 5 ms; gem5, with a marker around every
operation, took 657 s. What each fidelity costs per operation, and which comparison with gem5 is
fair, is measured in [Evaluation: the processor model](../../evaluation/sim_speed/cpu.md).
