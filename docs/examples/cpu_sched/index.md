---
title: Micro-scheduler on a CPU
parent: Examples
nav_order: 12
example_dir: examples/cpu_sched
summary: "Software on a calibrated processor, beside hardware: a micro-scheduler runs on the A53 platform model, keeps a ready list with the very algorithm the platform was calibrated on, and dispatches seeded Poisson jobs to SimpFun accelerators over AXI-Lite with interrupt completion. The report prices every scheduler operation -- latency, queueing, energy, footprint, confidence -- and the scheduler's trace is replayed on gem5 to check the model."
---

# Micro-scheduler on a CPU

The first user of the [processor model](../../guide/cpu/index.md): a scheduler that is *software*,
running on the calibrated Cortex-A53, handing jobs to accelerators that are *hardware*.

```bash
python -m examples.cpu_sched.cpu_sched --jobs 200 --accels 2
```

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
200 jobs in 158.1 us simulated; all correct: True
CPU utilization [0.749]; energy 19.277 uJ (dynamic 3.960, static 15.317)
  sched_ops.add      n  200  mean    617.3 ns  wait   358.1 ns     8791.2 pJ/call  EXTRAPOLATED {'EXTRAPOLATED': 30, 'INTERPOLATED': 170}  ws 384.0 l1  code 692 B
  sched_ops.delete   n  200  mean    397.0 ns  wait   182.0 ns     7906.9 pJ/call  INTERPOLATED {'INTERPOLATED': 200}  ws 392.0 l1  code 692 B
  dispatch           n  200  mean    348.8 ns  wait   258.5 ns     1947.7 pJ/call  INTERPOLATED {'INTERPOLATED': 200}  ws None None  code 112 B
  sched_ops.reprio   n   11  mean   1644.5 ns  wait  1140.9 ns    20957.9 pJ/call  INTERPOLATED {'INTERPOLATED': 11}  ws 376.0 l1  code 692 B
```

`sched_ops.add` reads `EXTRAPOLATED` for exactly the 30 arrivals that found the list empty: the
model was fitted on lists of 1 to 1,024 entries. The list peaks at 48 jobs. Static power dominates
the energy because the core idles a quarter of the time. At 0.3 µs the system cannot keep up: the
list grows to 173 jobs and draining it takes 298 µs.

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
around all of them, gem5 measured 63,266
cycles and the model predicted 75,656: **19.6 % high**. The model was calibrated one isolated call at
a time, and a scheduler running back to back pays less per call — roughly 20–40 cycles less. That is
a documented limit of the shipped platform; see [Calibration](../../guide/cpu/calibration.md).

Pricing the same trace through the model took about 9 ms; gem5 took 657 s.
