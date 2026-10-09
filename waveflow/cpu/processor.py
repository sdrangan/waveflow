"""processor.py — :class:`Processor`, a loosely-timed general-purpose processor.

A ``Processor`` runs :class:`~waveflow.cpu.task.SwFunction` calls on ``n_cores`` cores.  Each call is
a *task*: it waits in a ready queue, is granted a core, runs its Python body **once** at the moment
of that first grant, and then holds the core for the cycles its cost model predicts — in **one**
timed event, not one per instruction.  That is the loosely-timed bargain the rest of Waveflow makes
(``docs/guide/timing_model/models.md``): the function's result is computed all at once, and only its
timestamps are modelled.

WHY NOT A SIMPY RESOURCE.  ``simpy.PriorityResource`` keeps its waiters in a ``SortedQueue`` that
re-sorts the whole queue on every request, so a burst of N submissions costs O(N² log N): measured
in planning, 218 tasks/s with 20,000 queued (SimPy 4.1.2, Python 3.12.3).  It also has no notion of
*which* core a request got, and the context-switch charge depends on exactly that (a core that
already holds the calling thread's context pays nothing).  So the ready queue is a ``heapq`` keyed
``(prio, seq)`` — lower ``prio`` is more urgent, SimPy's convention, and ``seq`` keeps equal
priorities first-come first-served — and each core is a token that remembers whose context it holds.

WHOSE CONTEXT.  A software thread is the SimPy process that submits the calls.  A core pays
``switch_cycles`` when it starts a call of a different thread than the one whose context it holds,
and a free core that already holds the caller's context is granted first.  An interrupt handler is a
thread of its own.  Under run-to-completion an interrupt still takes the core at once; the call it
interrupted resumes on that core after the handler, before anything queued.

WHY THE RESULT WAITS.  The body runs at first grant, so its side effects (a write to a Python
structure, say) happen then; but the *result* is handed back only when the charged time has elapsed.
Nothing downstream can see an answer before the processor could have produced it.

Timing is never charged twice: bus and memory-mapped traffic a task issues goes through the
``MMIFMaster`` it holds and is timed by the bus model, outside the task.  A task that talks to
hardware is split at each interaction — one ``execute`` per stretch of pure computation.
"""

from __future__ import annotations

import bisect
import heapq
import itertools
import math
from dataclasses import dataclass, field
from typing import Any

import simpy

from waveflow.cpu.config import IRQ_PRIO, CpuConfig
from waveflow.cpu.report import CpuReport, build_report
from waveflow.cpu.task import SwFunction, TaskRecord, eval_cost, eval_cycles
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
    #: The software thread the call belongs to: the SimPy process that submitted it (a fresh object
    #: for an interrupt, or for a call made outside any process).  A core switching between owners
    #: pays a context switch; consecutive calls of one owner on one core do not.
    owner: object = None
    started: bool = False
    result: Any = None
    #: Compute cycles still to run (a preemption leaves the remainder here).
    left: float = 0.0
    #: In the ready queue (as opposed to holding a core, or finished).
    queued: bool = True


@dataclass(eq=False)
class _Core:
    """A core token: what it runs, whose context it holds (for the switch charge), what waits on it."""

    index: int
    #: The owner whose context the core holds; ``None`` after a switch cut short (no one's).
    last_owner: object = None
    running: _Task | None = None
    proc: simpy.Process | None = None
    #: When the running segment's compute (after its switch) begins.
    work_start: float = 0.0
    seg_start: float = 0.0
    seg_switch: float = 0.0
    busy_s: float = 0.0
    #: An interrupt has been sent to this core's segment and not yet delivered.
    preempt_pending: bool = False
    #: The interrupt handler that will take this core when the interrupt is delivered.
    irq_pending: _Task | None = None
    #: Run-to-completion victims of interrupts, resumed on this core after their handlers (LIFO).
    resume: list[_Task] = field(default_factory=list)


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
        # Free core indices, kept sorted: the lowest-numbered free core is granted first, unless a
        # free core still holds the task's owner's context (no switch to pay there).
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
        return build_report(
            self.config.name,
            elapsed,
            busy,
            self.records,
            static_power_mw=self.config.static_power_mw,
            config=self.config,
        )

    # ------------------------------------------------------------------
    # Submitting software
    # ------------------------------------------------------------------

    def execute(
        self, func: SwFunction, *args: Any, prio: int = 0, **kw: Any
    ) -> ProcessGen[Any]:
        """Run *func* on a core and return its result once the charged time has elapsed.

        ``prio`` is the call's **scheduling** priority (lower is more urgent) and is consumed here; it
        never reaches *func*.  A function that needs its own priority argument must name it
        something else (``examples/cpu_sched`` passes a job's priority as ``tg_prio``).
        """
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
        env = self.env
        seq = next(self._seq)
        # The software thread is the calling process; an interrupt is always a context of its own.
        owner = object() if is_irq else (env.active_process or object())
        rec = TaskRecord(
            name=func.name, prio=prio, t_submit=float(env.now), is_irq=is_irq, func=func
        )
        task = _Task(
            seq=seq,
            prio=prio,
            func=func,
            args=args,
            kw=kw,
            rec=rec,
            done=env.event(),
            owner=owner,
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
        if task.rec.is_irq:
            # An interrupt is taken on the core it interrupts, not through the queue: it leaves the
            # ready queue here (lazily -- its heap entry is skipped) and starts when the
            # preemption is delivered.
            task.queued = False
            victim.irq_pending = task
        victim.proc.interrupt("preempt")  # type: ignore[union-attr]

    # ------------------------------------------------------------------
    # Scheduling
    # ------------------------------------------------------------------

    def _dispatch(self) -> None:
        """Hand free cores to the most urgent ready tasks."""
        while self._ready:
            _, _, task = self._ready[0]
            if not task.queued:  # an interrupt taken directly on its victim's core
                heapq.heappop(self._ready)
                continue
            if not self._free:
                return
            heapq.heappop(self._ready)
            # Prefer a free core that still holds this owner's context; else the lowest-numbered.
            free, owner = self._free, task.owner
            index = free[0]
            if len(free) > 1 and self._cores[index].last_owner is not owner:
                for i in free:
                    if self._cores[i].last_owner is owner:
                        index = i
                        break
            free.remove(index)
            self._start(self._cores[index], task)

    def _start(self, core: _Core, task: _Task) -> None:
        task.queued = False
        core.running = task
        core.proc = self.env.process(self._segment(core, task))

    def _segment(self, core: _Core, task: _Task) -> ProcessGen[None]:
        """Hold *core* for *task*'s switch and remaining compute, then finish the task."""
        cfg = self.config
        rec = task.rec
        env = self.env
        now = float(env.now)
        if not task.started:
            task.started = True
            rec.t_start = now
            task.result, counters = task.func.fn(*task.args, **task.kw)
            rec.feats = task.func.features(counters, cfg)
            cycles = eval_cycles(task.func.cycles, rec.feats)
            if task.rec.is_irq:
                cycles += eval_cycles(cfg.irq_entry_cycles, {})
            task.left = cycles
            rec.cycles = cycles
            if task.func.energy_pj is not None:
                rec.energy_pj = eval_cost(task.func.energy_pj, rec.feats)
        same = core.last_owner is not None and core.last_owner is task.owner
        switch = 0.0 if same else eval_cycles(cfg.switch_cycles, {})
        core.seg_start = now
        core.seg_switch = switch
        core.work_start = now + switch * cfg.period
        try:
            yield env.timeout((switch + task.left) * cfg.period)
        except simpy.Interrupt:
            self._preempted(core, task)
            return
        rec.switch_cycles += switch
        task.left = 0.0
        self._complete(core, task)
        self._release(core)
        task.done.succeed(task.result)

    def _preempted(self, core: _Core, task: _Task) -> None:
        """Take *core* from *task*: re-queue it with the cycles it has left, or (an interrupt under
        run-to-completion scheduling) park it to resume on this core after the handler.

        ``executed = floor((now - work_start) * f_clk)`` whole cycles count as done (a fraction of a
        cycle is not progress; the time it took is still busy time).  A preemption that lands during
        the switch executes nothing, and the part of the switch already spent is lost -- the core is
        left holding no one's context, so whoever runs next pays a full switch.  A tolerance of
        ``1e-6`` cycles absorbs floating-point error, so an interrupt delivered at the very instant
        the work would have ended completes the task instead of re-queueing a zero-length remainder.
        A task is complete only once its switch is done, so one with no work left still resumes to
        finish its switch.
        """
        f = self.config.f_clk_hz
        rec = task.rec
        now = self.now
        switch_done = now >= core.work_start
        if switch_done:
            executed = math.floor((now - core.work_start) * f + 1e-6)
            rec.switch_cycles += core.seg_switch
            core.last_owner = task.owner
        else:
            executed = 0
            rec.switch_cycles += (now - core.seg_start) * f
            core.last_owner = None
        task.left = max(0.0, task.left - executed)
        isr, core.irq_pending = core.irq_pending, None
        core.preempt_pending = False
        if switch_done and task.left <= 1e-6:
            task.left = 0.0
            self._complete(core, task)
            if isr is not None:
                self._start(core, isr)
            else:
                self._release(core)
            task.done.succeed(task.result)
            return
        self._account(core, rec, now)
        rec.n_preempted += 1
        if isr is not None and not self.config.preemptive:
            # An interrupt is not a scheduling decision: the victim resumes here after the handler,
            # ahead of anything that became ready meanwhile.
            core.resume.append(task)
            self._start(core, isr)
            return
        task.queued = True
        heapq.heappush(self._ready, (task.prio, task.seq, task))
        if isr is not None:
            self._start(core, isr)
        else:
            self._release(core)

    def _complete(self, core: _Core, task: _Task) -> None:
        """Record *task* as finished on *core* (the caller then frees or reuses the core)."""
        rec = task.rec
        now = float(self.env.now)
        self._account(core, rec, now)
        core.last_owner = task.owner
        rec.t_end = now
        rec.core = core.index
        self.records.append(rec)

    def _account(self, core: _Core, rec: TaskRecord, now: float) -> None:
        """Charge the segment that just ended (at *now*) to the task and to the core."""
        dt = now - core.seg_start
        rec.busy_s += dt
        core.busy_s += dt

    def _release(self, core: _Core) -> None:
        core.running = None
        core.proc = None
        core.preempt_pending = False
        if (
            core.resume
        ):  # an interrupted run-to-completion task takes its core back first
            self._start(core, core.resume.pop())
            return
        bisect.insort(self._free, core.index)
        self._dispatch()
