"""mem_copy_runtime.py -- what a transaction costs in the pysim: N copies of B words each.

    python -m examples.mem_copy.mem_copy_runtime                  # the default grid
    python -m examples.mem_copy.mem_copy_runtime --n 1000 --b 16

memcpy has no compute, so the pysim's cost is all simulation: per copy, the command, three in-band
descriptors, a memory read, a memory write and a done message, each crossing timed streams and an
AXI-MM crossbar (:class:`~examples.mem_copy.mem_copy_sim.MemCopyTB`).  For each point it records:

- **pysim** -- ``run_sim`` alone, best of ``--reps`` runs, every copy checked bit-exact and one done
  message per copy;
- **events** -- the SimPy events per copy, from a separate counting run (counting slows it);
- **untimed** -- the same copies as numpy slice assignments, the baseline a plain model would pay.

Results go to ``mem_copy_runtime.json`` (``docs/evaluation/sim_speed/memcpy.md`` is written from it).
"""
from __future__ import annotations

import argparse
import collections
import contextlib
import io
import json
import platform
import tempfile
import time
from datetime import date
from pathlib import Path

import numpy as np

from examples.mem_copy.mem_copy import CopyJob
from examples.mem_copy.mem_copy_sim import MemCopySim

HERE = Path(__file__).resolve().parent
RESULTS = HERE / "mem_copy_runtime.json"
GRID = ((1, 16), (10, 16), (100, 16), (1000, 16), (1, 1024), (10, 1024), (100, 1024))


def jobs(n: int, b: int) -> tuple[CopyJob, ...]:
    """*n* copies of *b* words: sources in ``[0, n*b)``, destinations in ``[n*b, 2*n*b)``."""
    return tuple(CopyJob(src_off=j * b, dst_off=(n + j) * b, n_words=b) for j in range(n))


def _sim(n: int, b: int) -> MemCopySim:
    s = MemCopySim(jobs=jobs(n, b))
    return s


def time_pysim(n: int, b: int, reps: int) -> tuple[float, bool]:
    best, ok = None, True
    for _ in range(reps):
        s = _sim(n, b)
        with tempfile.TemporaryDirectory() as root, contextlib.redirect_stdout(io.StringIO()):
            s.write_scenario(root)
            t = time.perf_counter()
            s.tb.sim.run_sim()
            dt = time.perf_counter() - t
        tb = s.tb
        bpw = int(tb.mem_dwidth) // 8
        ok = ok and len(tb.done_sink.words) == n and all(
            np.array_equal(tb.mem._mem.read(j.dst_off * bpw, j.n_words).astype(np.uint64), e)
            for j, e in zip(tb._jobs, s.expected))
        best = dt if best is None else min(best, dt)
    return best, ok


def count_events(n: int, b: int) -> dict[str, float]:
    """SimPy events per copy, by event type."""
    import simpy.core as core
    kinds: collections.Counter = collections.Counter()
    orig = core.Environment.schedule

    def schedule(self, event, priority=1, delay=0):
        kinds[type(event).__name__] += 1
        return orig(self, event, priority, delay)

    s = _sim(n, b)
    core.Environment.schedule = schedule
    try:
        with tempfile.TemporaryDirectory() as root, contextlib.redirect_stdout(io.StringIO()):
            s.write_scenario(root)
            s.tb.sim.run_sim()
    finally:
        core.Environment.schedule = orig
    return {k: v / n for k, v in kinds.most_common()}


def time_untimed(n: int, b: int, reps: int) -> float:
    arena = np.zeros(2 * n * b, dtype=np.uint64)
    best = None
    for _ in range(reps):
        t = time.perf_counter()
        for j in range(n):
            arena[(n + j) * b:(n + j + 1) * b] = arena[j * b:(j + 1) * b]
        dt = time.perf_counter() - t
        best = dt if best is None else min(best, dt)
    return best


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--n", type=int, nargs="+", help="copy counts (with --b: a grid)")
    ap.add_argument("--b", type=int, nargs="+", help="words per copy")
    ap.add_argument("--reps", type=int, default=5, help="best of this many runs per point")
    a = ap.parse_args()
    grid = [(n, b) for n in a.n for b in a.b] if a.n and a.b else list(GRID)

    out = {"date": date.today().isoformat(),
           "machine": {"cpu": platform.processor(), "python": platform.python_version(),
                       "system": platform.platform()},
           "reps": a.reps, "points": []}
    print(f"{'N':>5} {'B':>5} {'pysim ms':>9} {'us/copy':>8} {'events/copy':>11} "
          f"{'untimed us/copy':>15} ok")
    for n, b in grid:
        sim_s, ok = time_pysim(n, b, a.reps)
        ev = count_events(n, b)
        un_s = time_untimed(n, b, a.reps)
        p = {"n": n, "b": b, "pysim_s": sim_s, "pysim_us_per_copy": 1e6 * sim_s / n,
             "events_per_copy": sum(ev.values()), "events_by_type": ev,
             "untimed_us_per_copy": 1e6 * un_s / n, "bits_exact": ok}
        out["points"].append(p)
        print(f"{n:5d} {b:5d} {1e3*sim_s:9.1f} {p['pysim_us_per_copy']:8.0f} "
              f"{p['events_per_copy']:11.1f} {p['untimed_us_per_copy']:15.2f} {ok}", flush=True)
    RESULTS.write_text(json.dumps(out, indent=1) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
