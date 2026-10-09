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

- **Jobs** arrive as a seeded Poisson stream (0.3 µs mean inter-arrival), each with a priority.
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
200 jobs in 137.8 us simulated; all correct: True
CPU utilization [0.759]; energy 16.141 uJ (dynamic 2.787, static 13.354)
  sched_ops.add      n  200  mean    294.9 ns  wait    75.4 ns     6045.2 pJ/call  EXTRAPOLATED {'EXTRAPOLATED': 35, 'INTERPOLATED': 165}  ws 144.0 l1  code 692 B
  sched_ops.delete   n  200  mean    267.1 ns  wait    90.9 ns     5315.1 pJ/call  INTERPOLATED {'INTERPOLATED': 200}  ws 152.0 l1  code 692 B
  dispatch           n  200  mean    262.3 ns  wait   151.6 ns     1947.7 pJ/call  INTERPOLATED {'INTERPOLATED': 200}  ws None None  code 112 B
  sched_ops.reprio   n   10  mean    782.6 ns  wait   446.3 ns    12537.7 pJ/call  INTERPOLATED {'INTERPOLATED': 10}  ws 144.0 l1  code 692 B
```

`sched_ops.add` reads `EXTRAPOLATED` for exactly the 35 arrivals that found the list empty: the
model was fitted on lists of 1 to 1,024 entries. Static power dominates the energy because the core
idles a quarter of the time.

## Two races it found

Building the example exposed the same mistake twice. The dispatcher first read the head of the list,
*then* called `delete`; but a call's body runs when it is granted a core, and with two dispatchers
waiting for the core both read the same head. One job ran twice and another was stranded, and the
run never ended. The aging pass made the same mistake with its target. Both choices now happen
inside the call, and `run()` raises unless every job is dispatched exactly once — the rule is on the
[scheduling page](../../guide/cpu/scheduling.md).

## Checking the model against gem5

The scheduler's operations are recorded as a trace (`--trace`), and
[`sched_replay.c`](../../../examples/cpu_sched/sched_replay.c) replays them on gem5's HPI core with
the same list algorithm (`python -m examples.cpu_sched.replay`). The replay sees exactly the trace's
counters. In one measured region around all 410 operations and 200 dispatches, gem5 measured 63,266
cycles and the model predicted 75,656: **19.6 % high**. The model was calibrated one isolated call at
a time, and a scheduler running back to back pays less per call — roughly 20–40 cycles less. That is
a documented limit of the shipped platform; see [Calibration](../../guide/cpu/calibration.md).

Pricing the same trace through the model took about 9 ms; gem5 took 657 s.
