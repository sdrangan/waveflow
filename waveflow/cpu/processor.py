"""processor.py — :class:`Processor`, a loosely-timed general-purpose processor.

A ``Processor`` runs :class:`~waveflow.cpu.task.SwFunction` calls on ``n_cores`` cores.  Each call is
a *task*: it waits in a ready queue, is granted a core, runs its Python body **once** at the moment
of that first grant, and then holds the core for the cycles its cost model predicts — in **one**
timed event, not one per instruction.  That is the loosely-timed bargain the rest of Waveflow makes
(``docs/guide/timing_model/models.md``): the function's result is computed all at once, and only its
timestamps are modelled.

WHY NOT A SIMPY RESOURCE.  ``simpy.PriorityResource`` keeps its waiters in a ``SortedQueue`` that
re-sorts the whole queue on every request, so a burst of N submissions costs O(N² log N): measured
in planning, 218 tasks/s with 20,000 queued.  It also has no notion of *which* core a request got,
and the context-switch charge depends on exactly that (a core resuming the task it ran last pays
nothing).  So the ready queue is a ``heapq`` keyed ``(prio, seq)`` — lower ``prio`` is more urgent,
SimPy's convention, and ``seq`` keeps equal priorities first-come first-served — and each core is a
token that remembers the last task it ran.

WHY THE RESULT WAITS.  The body runs at first grant, so its side effects (a write to a Python
structure, say) happen then; but the *result* is handed back only when the charged time has elapsed.
Nothing downstream can see an answer before the processor could have produced it.

Timing is never charged twice: bus and memory-mapped traffic a task issues goes through the
``MMIFMaster`` it holds and is timed by the bus model, outside the task.  A task that talks to
hardware is split at each interaction — one ``execute`` per stretch of pure computation.
"""

from __future__ import annotations

import heapq
import itertools
import math
from dataclasses import dataclass, field
from typing import Any

import simpy

from waveflow.cpu.config import IRQ_PRIO, CpuConfig
from waveflow.cpu.report import CpuReport, build_report
from waveflow.cpu.task import SwFunction, TaskRecord, eval_cycles
from waveflow.simulation.simobj import ProcessGen, SimObj


@dataclass(eq=False)
class _Task:
    """One submitted call, while it is queued or running."""

    seq: int
    prio: int
    func: SwFunction
    args: tuple
    kw: dict
    rec: TaskRecord
    done: simpy.Event
    started: bool = False
    result: Any = None
    #: Compute cycles still to run (a preemption leaves the remainder here).
    left: float = 0.0
    #: In the ready queue (as opposed to holding a core, or finished).
    queued: bool = True


@dataclass(eq=False)
class _Core:
    """A core token: what it is running and the last task it ran (for the switch charge)."""

    index: int
    last_seq: int = -1
    running: _Task | None = None
    proc: simpy.Process | None = None
    #: When the running segment's compute (after its switch) begins.
    work_start: float = 0.0
    seg_start: float = 0.0
    seg_switch: float = 0.0
    busy_s: float = 0.0
    #: An interrupt has been sent to this core's segment and not yet delivered.
    preempt_pending: bool = False


@dataclass(kw_only=True, eq=False)
class Processor(SimObj):
    """N cores, a priority ready queue, a context-switch charge, optional preemption.

    Run software with ``result = yield from cpu.execute(func, *args, prio=p)``.  Every finished task
    leaves a :class:`~waveflow.cpu.task.TaskRecord` in :attr:`records`.
    """

    config: CpuConfig = field(default_factory=CpuConfig)

    def __post_init__(self) -> None:
        super().__post_init__()
        self._ready: list[tuple[int, int, _Task]] = []
        self._seq = itertools.count()
        self._cores = [_Core(i) for i in range(self.config.n_cores)]
        # A heap of free core indices: the lowest-numbered free core is granted first.
        self._free: list[int] = list(range(self.config.n_cores))
        #: One record per finished task, in completion order.
        self.records: list[TaskRecord] = []

    def report(self, elapsed_s: float | None = None) -> CpuReport:
        """Utilization, latency, queueing and confidence over the run so far.

        *elapsed_s* is the horizon utilization is measured over; it defaults to the current time.
        A segment still running counts only once it ends.
        """
        elapsed = self.now if elapsed_s is None else float(elapsed_s)
        busy = [c.busy_s for c in self._cores]
        return build_report(self.config.name, elapsed, busy, self.records)

    # ------------------------------------------------------------------
    # Submitting software
    # ------------------------------------------------------------------

    def execute(
        self, func: SwFunction, *args: Any, prio: int = 0, **kw: Any
    ) -> ProcessGen[Any]:
        """Run *func* on a core and return its result once the charged time has elapsed."""
        task = self._submit(func, args, kw, prio, is_irq=False)
        result = yield task.done
        return result

    def interrupt(self, handler: SwFunction, *args: Any, **kw: Any) -> ProcessGen[Any]:
        """Run *handler* as an interrupt: the most urgent priority, plus the entry overhead."""
        task = self._submit(handler, args, kw, IRQ_PRIO, is_irq=True)
        result = yield task.done
        return result

    def compute(
        self, cycles: float, *, name: str = "compute", prio: int = 0
    ) -> ProcessGen[None]:
        """Charge *cycles* of computation with no body — the model's ``compute(cycles)``."""
        yield from self.execute(SwFunction.fixed(name, cycles), prio=prio)

    def _submit(
        self, func: SwFunction, args: tuple, kw: dict, prio: int, is_irq: bool
    ) -> _Task:
        seq = next(self._seq)
        rec = TaskRecord(
            name=func.name, prio=prio, t_submit=self.now, is_irq=is_irq, func=func
        )
        task = _Task(
            seq=seq,
            prio=prio,
            func=func,
            args=args,
            kw=kw,
            rec=rec,
            done=self.env.event(),
        )
        heapq.heappush(self._ready, (prio, seq, task))
        self._dispatch()
        if task.queued and (is_irq or self.config.preemptive):
            self._preempt_for(task)
        return task

    def _preempt_for(self, task: _Task) -> None:
        """Interrupt the least urgent running task, if *task* is strictly more urgent than it.

        Strictly: an equal priority never preempts, which keeps equal priorities first-come first-
        served.  One arrival preempts at most one core, and a core already being preempted is not
        chosen twice.  The freed core goes to whatever is most urgent when it is released -- normally
        *task*; if another core frees first and takes *task*, the preemption still happens (the
        victim re-queues and loses only its switch), which is the price of deciding at arrival.
        """
        victim = None
        for core in self._cores:
            run = core.running
            if run is None or core.preempt_pending or core.proc is None:
                continue
            if not core.proc.is_alive:
                continue
            if victim is None or (run.prio, run.seq) > (
                victim.running.prio,  # type: ignore[union-attr]
                victim.running.seq,  # type: ignore[union-attr]
            ):
                victim = core
        if victim is None or victim.running is None:
            return
        if victim.running.prio <= task.prio:
            return
        victim.preempt_pending = True
        victim.proc.interrupt("preempt")  # type: ignore[union-attr]

    # ------------------------------------------------------------------
    # Scheduling
    # ------------------------------------------------------------------

    def _dispatch(self) -> None:
        """Hand free cores to the most urgent ready tasks."""
        while self._free and self._ready:
            core = self._cores[heapq.heappop(self._free)]
            _, _, task = heapq.heappop(self._ready)
            task.queued = False
            core.running = task
            core.proc = self.env.process(self._segment(core, task))

    def _segment(self, core: _Core, task: _Task) -> ProcessGen[None]:
        """Hold *core* for *task*'s switch and remaining compute, then finish the task."""
        cfg = self.config
        rec = task.rec
        if not task.started:
            task.started = True
            rec.t_start = self.now
            task.result, counters = task.func.fn(*task.args, **task.kw)
            rec.feats = task.func.features(counters, cfg)
            cycles = eval_cycles(task.func.cycles, rec.feats)
            if task.rec.is_irq:
                cycles += eval_cycles(cfg.irq_entry_cycles, {})
            task.left = cycles
            rec.cycles = cycles
        switch = (
            0.0 if core.last_seq == task.seq else eval_cycles(cfg.switch_cycles, {})
        )
        core.seg_start = self.now
        core.seg_switch = switch
        core.work_start = self.now + switch * cfg.period
        try:
            yield self.env.timeout((switch + task.left) * cfg.period)
        except simpy.Interrupt:
            self._preempted(core, task)
            return
        rec.switch_cycles += switch
        task.left = 0.0
        self._finish(core, task)

    def _preempted(self, core: _Core, task: _Task) -> None:
        """Re-queue *task* with the cycles it has left; free *core*.

        ``executed = floor((now - work_start) * f_clk)`` whole cycles count as done (a fraction of a
        cycle is not progress; the time it took is still busy time).  A preemption that lands during
        the switch executes nothing, and the part of the switch already spent is lost -- the core is
        left holding no one's context, so whoever runs next pays a full switch.  A tolerance of
        ``1e-6`` cycles absorbs floating-point error, so an interrupt delivered at the very instant
        the work would have ended completes the task instead of re-queueing a zero-length remainder.
        """
        f = self.config.f_clk_hz
        rec = task.rec
        now = self.now
        if now >= core.work_start:
            executed = math.floor((now - core.work_start) * f + 1e-6)
            rec.switch_cycles += core.seg_switch
            core.last_seq = task.seq
        else:
            executed = 0
            rec.switch_cycles += (now - core.seg_start) * f
            core.last_seq = -1
        task.left = max(0.0, task.left - executed)
        if task.left <= 1e-6:
            task.left = 0.0
            self._finish(core, task)
            return
        self._account(core, rec)
        rec.n_preempted += 1
        task.queued = True
        heapq.heappush(self._ready, (task.prio, task.seq, task))
        self._release(core)

    def _finish(self, core: _Core, task: _Task) -> None:
        rec = task.rec
        self._account(core, rec)
        core.last_seq = task.seq
        rec.t_end = self.now
        rec.core = core.index
        self.records.append(rec)
        self._release(core)
        task.done.succeed(task.result)

    def _account(self, core: _Core, rec: TaskRecord) -> None:
        """Charge the segment that just ended to the task and to the core."""
        dt = self.now - core.seg_start
        rec.busy_s += dt
        core.busy_s += dt

    def _release(self, core: _Core) -> None:
        core.running = None
        core.proc = None
        core.preempt_pending = False
        heapq.heappush(self._free, core.index)
        self._dispatch()
