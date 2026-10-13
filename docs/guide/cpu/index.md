---
title: Processor Model
parent: Guide
nav_order: 12.75
has_children: true
audience: python
api: [Processor, CpuConfig, SwFunction, TaskRecord, CpuReport, CpuPlatform, CpuAreaModel]
summary: "A loosely-timed, calibrated model of a general-purpose processor for the software a system runs beside its accelerators: schedulers, bookkeeping, irregular control code. Software is Python that reports the work it did as counters; a cost model calibrated on gem5 turns the counters into cycles and energy; a Processor schedules the calls over N cores with priorities, context switches and optional preemption, all in the same SimPy simulation as the hardware. Ships with a calibrated Cortex-A53 platform (the RFSoC 4x2's, gem5 HPI, McPAT 22 nm)."
---

# Processor Model

Some of a system's work belongs on a general-purpose core, not an accelerator: a scheduler, a
control loop, bookkeeping that branches on every element. `waveflow.cpu` models that core the way
Waveflow models hardware — **loosely timed**: the software's result is computed at once, in Python,
and only its *duration* is simulated.

```text
  Python function ──► (result, counters) ──► cost model ──► cycles, energy ──► Processor charges
  (your software)      "scanned 12, moved 3"   (calibrated)                       one timed event
```

## The three pieces

- **[`SwFunction`](../../../waveflow/cpu/task.py)** — a piece of software. Its function returns its
  result *and* the work it did, as counters (items scanned, elements moved, samples processed). Its
  cost models map those counters to cycles and to dynamic energy in pJ. Counters, not input size,
  because control code costs what it *did*: a scheduler that finds its task at the head of a queue is
  cheap however long the queue is.
- **[`Processor`](../../../waveflow/cpu/processor.py)** — a `SimObj` with N cores, a priority ready
  queue, a context-switch cost and optional preemption. `yield from cpu.execute(func, *args)` runs
  the function when a core is granted and returns its result once the charged time has elapsed. See
  [Scheduling](scheduling.md).
- **A calibrated platform** — the cost models, measured and fitted. The shipped one is the Cortex-A53
  of the RFSoC 4x2, measured on gem5 and priced by McPAT. See [Calibration](calibration.md), and
  [Using it in a DSE](dse.md) for the configuration, area and leakage side.

## What it charges, and what it does not

The processor charges **compute**. Bus and memory-mapped traffic a task issues goes through its
`MMIFMaster` and is timed by the bus model, outside the task — never twice. A task that talks to
hardware is split at each interaction: one `execute` per stretch of pure computation.

Every number it reports carries a confidence (`EXACT`, `INTERPOLATED`, `EXTRAPOLATED`,
`UNCALIBRATED`), computed when a report is asked for, so a design-space exploration can see when a
configuration has left the region the models were measured over.

## Pages

- [Scheduling](scheduling.md) — cores, priorities, context switches, preemption, interrupts, the report.
- [Calibration](calibration.md) — how the A53 platform was measured, and how accurate it is.
- [Using it in a DSE](dse.md) — configurations, area and leakage, confidence, and the known limits.
- The worked example: [a micro-scheduler on the A53](../../examples/cpu_sched/index.md).
