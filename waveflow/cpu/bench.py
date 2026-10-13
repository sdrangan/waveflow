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
"""

from __future__ import annotations

import argparse
import platform
import random
import time

from waveflow.cpu.config import CpuConfig
from waveflow.cpu.processor import Processor
from waveflow.cpu.task import SwFunction
from waveflow.simulation.simulation import Simulation

#: Cycles each benchmark task costs: about a microsecond of a 1.2 GHz core.
TASK_CYCLES = 1200


def run_bench(pattern: str, n_tasks: int, n_cores: int = 4, seed: int = 1) -> dict:
    """Simulate *n_tasks* tasks and return counts, simulated time and tasks per wall second."""
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
    }


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
