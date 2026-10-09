---
title: Scheduling
parent: Processor Model
nav_order: 1
audience: python
snippets: run
api: [Processor.execute, Processor.interrupt, Processor.compute, Processor.report, CpuConfig, TaskRecord, CpuReport, FunctionStats]
summary: "How a Processor runs software: N cores sharing one priority ready queue (lower number is more urgent, first-come first-served among equals), a context switch charged when a core starts a different task, optional preemption with an exact remaining-cycles rule, interrupts as the most urgent tasks, and the report: latency, queueing delay, utilization, energy, footprint and confidence per function."
---

# Scheduling

A `Processor` has `n_cores` cores and one ready queue. A call is a **task**: it waits in the queue,
is granted the lowest-numbered free core, runs its Python body **once** at that moment, and holds the
core for its predicted cycles in a single timed event. Its result is handed back only when that time
has elapsed, so nothing downstream sees an answer before the processor could have produced it.

## The rules

- **Priority.** `execute(func, *args, prio=p)`: a lower `p` is more urgent (SimPy's convention), and
  equal priorities are first-come first-served. `prio` is the call's *scheduling* priority and is
  consumed by `execute`; a function that needs its own priority argument must name it something else.
- **Context switch.** `CpuConfig.switch_cycles` is charged whenever a core starts a task other than
  the one it ran last — including its first task.
- **Preemption** (`CpuConfig(preemptive=True)`). A strictly more urgent arrival interrupts the least
  urgent running task. The victim keeps `floor((now - work_start) * f_clk)` whole cycles as done; a
  fraction of a cycle is busy time, not progress. Preempted during its switch, it has done nothing
  and the part of the switch already spent is lost. It re-enters the queue at its original priority
  and arrival order, and pays a switch when it resumes.
- **Interrupts.** `cpu.interrupt(handler)` runs at the most urgent priority, with
  `CpuConfig.irq_entry_cycles` added, and preempts even when the processor does not.
- **Decide inside the call.** The body runs at grant, possibly long after the call was made, so any
  choice about shared state — which job is at the head of a list — belongs in the body. A choice the
  caller made beforehand is stale by then; the [example](../../examples/cpu_sched/index.md) found two
  races exactly this way.

## An example

A long task at priority 5, preempted by an urgent one. At 1 Hz one cycle is one second, so the
timeline is the cycle count:

```python
from waveflow.cpu import CpuConfig, Processor, SwFunction
from waveflow.simulation.simulation import Simulation

sim = Simulation()
cfg = CpuConfig(f_clk_hz=1.0, switch_cycles=1, preemptive=True)
cpu = Processor(name="cpu", sim=sim, config=cfg)
work = SwFunction(name="work", fn=lambda: (None, {}), cycles=10)
urgent = SwFunction(name="urgent", fn=lambda: (None, {}), cycles=3)


def submit(at, func, prio):
    def proc():
        yield sim.env.timeout(at)
        yield from cpu.execute(func, prio=prio)

    sim.env.process(proc())


submit(0, work, prio=5)
submit(4, urgent, prio=1)
sim.env.run()
for r in cpu.records:
    print(f"{r.name:6s} start {r.t_start:g} end {r.t_end:g} busy {r.busy_s:g} "
          f"wait {r.wait_s:g} preempted {r.n_preempted}")
```

`work` switches in (0–1) and has run 3 cycles when `urgent` arrives at 4. `urgent` switches in
(4–5) and runs 5–8. `work` resumes with 7 cycles left: switch 8–9, run 9–16.

```text
urgent start 4 end 8 busy 4 wait 0 preempted 0
work   start 0 end 16 busy 12 wait 4 preempted 1
```

`busy` counts every switch a task paid; `wait` — latency minus busy — is exactly its time in the
ready queue.

## The report

`cpu.report()` folds the finished tasks into a `CpuReport`:

| Field | What it is |
|---|---|
| `utilization` | per core, busy time over the elapsed time |
| `functions[name]` | per function: calls, cycles, mean and max latency, mean and max queueing delay |
| `.energy_pj`, `dynamic_pj`, `static_pj`, `total_pj` | dynamic energy per task, plus every core's static power over the run |
| `.code_bytes`, `.ws_max`, `.regime` | footprint: the function's code, its largest working set, and whether that lives in L1, L2 or DRAM |
| `.confidence`, `.levels` | the weakest call's confidence, and how many calls had each level |

Confidence is computed here, once per distinct feature point, not on the simulation's hot path. The
weakest level alone can hide that it was one call in two hundred, which is what `levels` shows.

## Speed

Each task is a handful of SimPy events. On an 8-thread i7-7700K under load, 4 cores ran about 40,000
tasks per wall-clock second with streaming arrivals and about 37,000 with 20,000 queued at once
(`python -m waveflow.cpu.bench`). The ready queue is a heap: SimPy's own priority resource re-sorts
its queue on every request and managed a few hundred per second at that depth.
