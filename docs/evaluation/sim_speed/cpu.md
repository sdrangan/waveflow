---
title: "Simulation speed: the processor model"
parent: Simulation speed
nav_order: 3
summary: "A scheduler's operations on the calibrated A53 model, run three ways on the same trace: the untimed Python with its calibrated prices (2.82 us an operation), Waveflow's Processor in the pysim (15.3 us), and gem5's HPI core (469 us of host time simulating the operation, 10.8 ms a run end to end). The pysim adds 12.5 us an operation, about 2.8 us per SimPy event; three optimization rounds took it from 31.8 us, every simulated outcome unchanged. What the remaining time is, why the ratio to the untimed model rose while both got faster, and what is left."
---

# Simulation speed: the processor model

The [processor model](../../guide/cpu/index.md) is the other kind of model Waveflow simulates: not a
stream of samples through hardware, but software on a core. A call runs its Python body once,
reports what it did as counters, and is charged the cycles a calibrated cost model predicts, in one
timed event. This page measures what that costs, the way the [FFT page](fft.md) does for a DSP block.

## The set-up

The workload is the [micro-scheduler example](../../examples/cpu_sched/index.md)'s own trace: 200
jobs at its default arrival rate, **411 list operations** (`add`, `delete`, `reprio`), a dispatch
after every `delete`. The ready list holds 16.4 jobs on average and an operation scans or moves 19.4
entries. The trace is repeated 1, 2 and 4 times back to back, with the job ids offset per copy: it
leaves the list empty, so every copy does the same work and the cost per operation is a constant to
fit. (The example's own traces at 100, 200 and 400 jobs would not do: at that arrival rate the list
keeps growing, and the work per operation with it.)

```text
untimed:  trace --> list operations (calibrated twins) + prices (cost models) --> summed cycles, energy
pysim:    one software thread --> Processor (ready queue, core, switches) --> the same bodies, priced  (SimPy)
gem5:     sched_replay.c --> gem5 v25.1, HPI core (the A53 at 1.2 GHz), syscall emulation, in Docker
```

- **Untimed** runs every operation on a real Python list with the twins the A53 platform was
  calibrated on, and prices it with the platform's fitted models -- the estimate a system engineer
  adds up without a simulator: no queueing, no context switch.
- **The pysim** runs the very same bodies as calls on a `Processor` (`waveflow.cpu.bench.SchedTrace`),
  so the difference between the two is the simulation's own cost. The untimed sum and the cycles the
  `Processor` charges agree to 1e-9.
- **gem5** is the ground truth: the same list operations, compiled for AArch64, replayed on gem5's
  HPI core with one measured region around the whole pass. Its cost is reported two ways: the host
  time gem5 spent simulating the measured pass, and the run end to end (starting the container,
  parsing the trace, a warm-up pass over it, the measured pass, printing three counters per
  operation).
- The **whole example** is a fourth row, per job: the scheduler on the A53, two accelerators behind
  AXI-Lite, and their interrupts, at 0.8 us between arrivals (where the work per job stays flat).

```bash
python -m examples.cpu_sched.cpu_sched_runtime --gem5 --profile   # -> examples/cpu_sched/runtime.json
```

## Results

Per scheduler operation, fitted over 411, 822 and 1644 operations; every run checked (the counters
equal the trace's, untimed and pysim charge the same cycles, gem5's counters match):

| | per operation | one-time cost |
|---|---|---|
| untimed (Python operations + calibrated prices) | **2.82 us** | none |
| pysim (`Processor`) | **15.3 us** | none |
| gem5 HPI, simulating the measured pass | 469 us | 0.25 s per run |
| gem5 HPI, a whole run | 10.8 ms | 5.4 s per run; building the replay 0.16 s |

| also measured | |
|---|---|
| the whole example, per job (2.03 scheduler operations, the bus, two accelerators) | **0.354 ms**, plus 6.5 ms to build |
| `waveflow.cpu.bench`, 4 cores: streaming arrivals / 20,000 queued at once | 76,042 / 76,659 tasks a second |
| SimPy events per operation / per job of the example / per bench task (stream, burst) | 4.46 / 48.3 / 6.0, 5.0 |

Measured 2026-10-19 on Linux with an Intel Core i7-7700K (8 threads, 4.2 GHz), Python 3.12.3,
SimPy 4.1.2, numpy 2.5.3, gem5 v25.1.0.1; the machine idle (load below 1) before each series, the
best of three runs per point (of fifteen for a run under 50 ms). This is not the machine the
[memcpy](memcpy.md) and [FFT](fft.md) pages were measured on.

**Against gem5, which comparison?** The pysim is **31x** faster than gem5's simulation of the same
pass, and **705x** faster than a gem5 run, which is what a designer waits for. A third figure is in
the processor model's history: 72,252x, against gem5 with a marker around *every* operation, where
writing gem5's statistics at each marker dominates its time. All three are true; only the first
compares simulation with simulation.

**The answers are not the same, and are not meant to be.** gem5 counted 86,958 cycles for the 411
operations, about 210 an operation; the calibrated models charge 97,076, +11.6 % (+12.3 % at four
copies). How accurate the platform is, and why it over-predicts back-to-back operations, is on
[Calibration](../../guide/cpu/calibration.md); this page measures what each fidelity costs to run.

## Where the time goes

Profiled at 1644 operations (cProfile, which inflates call-heavy code, so the shares are indicative;
the timings above never use it). Time in numpy and the standard library is charged to the code that
called it:

| | untimed | pysim | the whole example |
|---|---|---|---|
| processor model: ready queue, cores, accounting | -- | 46% | 9% |
| SimPy itself | -- | 26% | 28% |
| the software: list operations, the driver | 58% | 18% | 5% |
| pricing: features, cost models | 42% | 9% | 4% |
| bus, registers, accelerators | -- | -- | 51% |

So the pysim's **12.5 us** an operation on top of the untimed model is the simulation: the
`Processor`'s bookkeeping and SimPy, about **2.8 us per event** at 4.46 events an operation. A call
costs three events -- its start, its timed completion, and the event its caller waits on -- and an
operation is 1.49 calls (a `delete` brings its dispatch). The whole example spends half its time in
the bus and register model of the accelerators' AXI-Lite links, which is framework code, not the
processor model's.

## How it got here

Three rounds, the plan's limit; each measured as interleaved A/B runs in fresh processes (five pairs,
the best of each) against the commit before it, and each leaving every simulated `TaskRecord`
unchanged -- of the trace, of the bench, and of the whole example at two arrival rates
(`tests/cpu/test_sim_speed_outcomes.py`, `tests/examples/test_cpu_sched_outcomes.py`):

| round | change | pysim, us an operation | untimed, us an operation | whole example, ms a job |
|---|---|---|---|---|
| 1 | a task's time on a core is one timeout with a callback, not a spawned SimPy process | 31.8 → 29.4 | 10.95 → 10.83 | 0.402 → 0.395 |
| 2 | a call's price is memoized per feature point (`SwFunction.price`) | 29.0 → 15.9 | 10.78 → 2.78 | 0.397 → 0.363 |
| 3 | the processor's bookkeeping: a direct start when nothing is queued, slots, the switch cost once | 15.9 → 14.9 | 2.81 → 2.79 | 0.368 → 0.361 |

Each row is one A/B session, so a row's "before" is not exactly the previous row's "after": the
machine is shared, and its load moved between sessions. Round 1 removed two events of four from
every call (5.95 events an operation became 4.47).

## Insights

- **The pysim costs 15.3 us a scheduler operation, 12.5 us of it the simulation.** A scheduler that
  runs a hundred thousand operations is simulated in under two seconds.
- **The ratio to the untimed model went up, from 2.9x to 5.4x, while both got faster.** Round 2
  made the model itself faster -- pricing was most of the untimed sum, and a scheduler's counters
  repeat -- and the pysim runs the same pricing. As on the FFT page, the overhead is the measure of
  the simulation, and here it fell from 21 to 12.5 us an operation.
- **The overhead is per event, and the events are few.** Three per call is the floor for a call that
  takes simulated time and returns a result: a start, a completion, a wake-up. The start could be
  folded into the submit, but only if no other event of the same instant could see the difference.
- **For gem5, the comparison that matters is the cost of a design point.** gem5 needs its five
  seconds whatever the workload, and 469 us an operation after that; the pysim needs neither a build
  nor a start-up.
- **A system is dominated by what is around the processor.** In the whole example, 2.03 scheduler
  operations cost about 31 us of a job's 354; the rest is the bus, the accelerators and their
  interrupts.

## Lessons learned

For the next model whose pysim is slow:

1. **Pin the simulated outcome before changing anything.** Every `TaskRecord`'s times, core, cycles
   and energy, written as exact hex floats and hashed, against the unoptimized code. One extra cycle
   on a context switch failed all five pinned runs; all three rounds passed them unchanged.
2. **Prove the A/B harness before trusting it.** With the same code on both sides it gave 0.99-1.00,
   which set the noise floor under the 3 % a round must win by. It also exposed that `python -m`
   puts the working directory first on the path: setting `PYTHONPATH` to the old tree would have
   measured the new code twice.
3. **Make the workload stationary.** The example's traces at 100, 200 and 400 jobs were not the same
   work: the list grew with the job count. Repeating one trace kept the work per operation exact.
4. **Name the comparison.** 31x, 705x and 72,252x against gem5 are all true.
5. **Report the overhead, not only the ratio.** A rule of "within 2x of the untimed model" penalizes
   making the model faster; the overhead per operation and the absolute times say what changed.
6. **A process per call is the expensive way to wait.** The memcpy lesson again: a spawned SimPy
   process costs an object, a generator and two events; a timeout with a callback does the same job.
7. **Short runs need more repetitions.** A 5 ms point measured best of three moved the untimed fit by
   26 % on a shared machine; best of fifteen for runs under 50 ms held it.

## Future work

- **The rest of the whole example**: the AXI-Lite register path -- building and parsing its
  messages (`dataschema`'s `_convert`, `_layout_memo` and `serialize` lead the profile), the memory
  interface and the register map -- is framework code, left for a change of its own.
- **The start event**: running a body directly at submit, when nothing else is pending at that
  instant, would remove a third of the events; it needs an argument that the order is unchanged.
- **Other workloads**: several cores, preemptive scheduling and interrupt-heavy software are not
  measured here; the bench's two patterns are the only multi-core figures.
