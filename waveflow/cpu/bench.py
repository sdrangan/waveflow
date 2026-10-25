"""bench.py — how many tasks per wall-clock second the processor model simulates.

Two arrival patterns, because they stress different things:

* ``stream`` — Poisson arrivals at 80 % offered load over all cores, so the ready queue stays short
  and the cost is the per-task event work (a submit, a grant, one timeout, a completion).
* ``burst`` — every task submitted at ``t = 0``, so the ready queue is N deep and the cost of the
  queue itself shows.  A queue that re-sorts on each request is O(N² log N) here; a heap is
  O(N log N).

Each submission is its own SimPy process waiting on ``execute`` — the way a model with many
concurrent callers uses it.  Wall time is measured around ``env.run()`` only, so building the
submissions is not counted::

    python -m waveflow.cpu.bench --pattern stream --tasks 100000
    python -m waveflow.cpu.bench --pattern burst --tasks 20000

:class:`SchedTrace` is the third workload: a scheduler's trace of list operations (the
``examples/cpu_sched`` micro-scheduler's), replayed on a real list with the calibrated twins and
priced by a platform -- either untimed, summing the prices, or through a :class:`Processor`.  The two
run the same Python, so their difference is the cost of the simulation itself
(``docs/evaluation/sim_speed/cpu.md``).
"""

from __future__ import annotations

import argparse
import platform
import random
import time
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Any

from waveflow.cpu.calib.kernels.sched_ops import _Counters, tg_insert, tg_remove
from waveflow.cpu.config import CpuConfig
from waveflow.cpu.processor import Processor
from waveflow.cpu.task import SwFunction
from waveflow.simulation.simulation import Simulation

if TYPE_CHECKING:
    from waveflow.cpu.platform import CpuPlatform

#: Cycles each benchmark task costs: about a microsecond of a 1.2 GHz core.
TASK_CYCLES = 1200


def run_bench(pattern: str, n_tasks: int, n_cores: int = 4, seed: int = 1) -> dict:
    """Simulate *n_tasks* tasks and return counts, simulated time and tasks per wall second.

    ``records`` holds the processor's :class:`~waveflow.cpu.task.TaskRecord` list, in completion
    order: the simulated outcome, which an optimization must leave unchanged.
    """
    if pattern not in ("stream", "burst"):
        raise ValueError(f"unknown pattern {pattern!r}")
    sim = Simulation()
    cfg = CpuConfig(n_cores=n_cores, switch_cycles=50)
    cpu = Processor(name="cpu", sim=sim, config=cfg)
    func = SwFunction(name="task", fn=lambda: (None, {}), cycles=TASK_CYCLES)
    env = sim.env

    if pattern == "burst":
        for _ in range(n_tasks):
            env.process(cpu.execute(func))
    else:
        rng = random.Random(seed)
        service_s = (TASK_CYCLES + 50) * cfg.period
        rate = 0.8 * n_cores / service_s

        def arrivals():
            for _ in range(n_tasks):
                yield env.timeout(rng.expovariate(rate))
                env.process(cpu.execute(func))

        env.process(arrivals())

    t0 = time.perf_counter()
    env.run()
    wall = time.perf_counter() - t0
    done = len(cpu.records)
    return {
        "pattern": pattern,
        "tasks": done,
        "cores": n_cores,
        "sim_s": env.now,
        "wall_s": wall,
        "tasks_per_s": done / wall if wall > 0 else float("inf"),
        "python": platform.python_version(),
        "records": cpu.records,
    }


#: The operations of a scheduler trace, each priced by the platform family ``sched_ops.<op>``.
TRACE_OPS = ("add", "delete", "reprio")
#: The counters each operation reports -- its cost model's features.
TRACE_COUNTERS = ("n_tasks", "n_scanned", "n_moved")


def repeat_trace(
    trace: Iterable[Mapping[str, Any]], copies: int
) -> list[dict[str, Any]]:
    """*copies* copies of *trace* back to back, job ids offset per copy.

    A trace that leaves its list empty (every job added is dispatched) replays the same way in every
    copy, so the work per operation is the same whatever *copies* is.
    """
    one = [dict(t) for t in trace]
    span = 1 + max((int(t["gid"]) for t in one), default=-1)
    return [
        {**t, "gid": int(t["gid"]) + k * span} for k in range(int(copies)) for t in one
    ]


class SchedTrace:
    """A scheduler trace replayed on a real list: untimed (:meth:`untimed`) or timed (:meth:`pysim`).

    *trace* is the ``examples/cpu_sched`` micro-scheduler's, as ``replayable()`` leaves it: one dict
    per operation with ``op`` (``add``, ``delete`` or ``reprio``), ``gid``, ``tg_prio`` (``add`` and
    ``reprio``) and the counters the operation saw.  Each operation runs the calibrated twin on a
    Python list, as ``sched_replay.c`` does on gem5: ``add`` inserts ``(gid, tg_prio)``, ``delete``
    removes ``gid`` and is followed by a ``dispatch`` call, ``reprio`` removes and re-inserts.  The
    prices are the platform's, with the example's working set (8 bytes per list entry), so a call
    costs here exactly what it costs in the example.
    """

    def __init__(
        self,
        trace: Iterable[Mapping[str, Any]],
        platform: CpuPlatform | None = None,
        n_cores: int = 1,
    ) -> None:
        from waveflow.cpu.platform import CpuPlatform

        self.trace = [dict(t) for t in trace]
        bad = [t for t in self.trace if t["op"] not in TRACE_OPS or int(t["gid"]) < 0]
        if bad:
            raise ValueError(f"not a replayable trace operation: {bad[0]}")
        self.platform = platform or CpuPlatform.load()
        self.config = self.platform.cpu_config(n_cores=n_cores)
        #: The list, as ``(gid, prio)`` sorted by ``(prio, gid)``; reset by each run.
        self.ready: list[tuple[int, int]] = []
        #: Each operation's counters, in the order the bodies ran; reset by each run.
        self.counters: list[dict[str, float]] = []
        self.funcs = {op: self._sw(op) for op in TRACE_OPS}
        model = self.platform.model
        self.dispatch = SwFunction(
            name="dispatch",
            fn=lambda: (None, {"n_dispatch": 1.0}),
            cycles=model("dispatch", "cycles"),
            energy_pj=model("dispatch", "energy_pj"),
        )

    def _sw(self, op: str) -> SwFunction:
        family = f"sched_ops.{op}"

        def body(gid: int, tg_prio: int = 0) -> tuple[int, dict[str, float]]:
            c = _Counters()
            n = len(self.ready)
            if op == "add":
                tg_insert(self.ready, (gid, tg_prio), c)
            elif op == "delete":
                tg_remove(self.ready, gid, c)
            else:
                tg_remove(self.ready, gid, c)
                tg_insert(self.ready, (gid, tg_prio), c)
            counters = {
                "n_tasks": float(n),
                "n_scanned": float(c.scanned),
                "n_moved": float(c.moved),
            }
            self.counters.append(counters)
            return gid, counters

        return SwFunction(
            name=family,
            fn=body,
            cycles=self.platform.model(family, "cycles"),
            energy_pj=self.platform.model(family, "energy_pj"),
            working_set=lambda cnt: 8.0 * (cnt["n_tasks"] + 1),
        )

    def _reset(self) -> None:
        self.ready = []
        self.counters = []

    def untimed(self) -> dict[str, Any]:
        """Run every operation and price it as the processor would -- with no simulation.

        Returns the summed compute ``cycles`` and dynamic ``energy_pj``, and the number of ``calls``.
        No queueing, no context switch: what an estimate without a simulator adds up.
        """
        self._reset()
        cfg = self.config
        cycles = energy = 0.0
        calls = 0
        for t in self.trace:
            for f, args in self._calls(t):
                _, cnt = f.fn(*args)
                _, c, e = f.price(cnt, cfg)
                cycles += c
                if e is not None:
                    energy += e
                calls += 1
        return {"cycles": cycles, "energy_pj": energy, "calls": calls}

    def pysim(self) -> Processor:
        """Run the trace through a :class:`Processor`, one software thread issuing it in order.

        Returns the processor, whose ``records`` are the simulated outcome.
        """
        self._reset()
        sim = Simulation()
        cpu = Processor(name="cpu", sim=sim, config=self.config)

        def thread():
            for t in self.trace:
                for f, args in self._calls(t):
                    yield from cpu.execute(f, *args)

        sim.env.process(thread())
        sim.env.run()
        return cpu

    def _calls(self, t: Mapping[str, Any]) -> list[tuple[SwFunction, tuple[Any, ...]]]:
        """The calls one trace operation makes: the operation, then a dispatch after a delete."""
        op, gid = t["op"], int(t["gid"])
        calls: list[tuple[SwFunction, tuple[Any, ...]]] = [
            (self.funcs[op], (gid, int(t.get("tg_prio", 0))))
        ]
        if op == "delete":
            calls.append((self.dispatch, ()))
        return calls

    def counters_match(self) -> bool:
        """The last run's counters equal the trace's: the same program ran on the same list."""
        if len(self.counters) != len(self.trace):
            return False
        return all(
            float(t[k]) == c[k]
            for t, c in zip(self.trace, self.counters)
            for k in TRACE_COUNTERS
        )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pattern", choices=("stream", "burst"), default="stream")
    ap.add_argument("--tasks", type=int, default=100_000)
    ap.add_argument("--cores", type=int, default=4)
    args = ap.parse_args(argv)
    r = run_bench(args.pattern, args.tasks, args.cores)
    print(
        f"{r['pattern']}: {r['tasks']} tasks on {r['cores']} cores, "
        f"{r['sim_s'] * 1e3:.3f} ms simulated, {r['wall_s']:.3f} s wall, "
        f"{r['tasks_per_s']:,.0f} tasks/s (Python {r['python']})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
