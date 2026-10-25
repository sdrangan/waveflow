"""cpu_sched_runtime.py -- what a scheduler operation costs: untimed Python, the Processor, gem5.

    python -m examples.cpu_sched.cpu_sched_runtime                 # untimed, pysim, bench, events
    python -m examples.cpu_sched.cpu_sched_runtime --gem5          # ... + the gem5 replay (Docker)
    python -m examples.cpu_sched.cpu_sched_runtime --profile       # ... + where the pysim's time goes

``docs/evaluation/sim_speed/cpu.md`` is written from what this records.  The workload is the
micro-scheduler's trace (:mod:`examples.cpu_sched.cpu_sched`, 200 jobs at the default 0.6 us
inter-arrival, ``replayable``), repeated ``--copies`` times with the job ids offset per copy
(:func:`~waveflow.cpu.bench.repeat_trace`).  The list is empty at the end of the trace, so every copy
does the same work and the cost per operation is a constant to fit.  The fidelities:

- **untimed** -- every operation run on a real list by the calibrated twin and priced by the
  platform's models, with a ``dispatch`` after every ``delete``: the sum a system engineer computes
  without a simulator (:meth:`~waveflow.cpu.bench.SchedTrace.untimed`);
- **pysim (trace)** -- the same bodies as calls on a :class:`~waveflow.cpu.processor.Processor`, one
  software thread issuing them in order (:meth:`~waveflow.cpu.bench.SchedTrace.pysim`).  Same Python
  as untimed, so the difference is the simulation's own cost;
- **pysim (system)** -- the whole example per job, the scheduler on the A53 and two accelerators
  behind AXI-Lite, at ``--system-interarrival`` (0.8 us: the list stays short, so the work per job
  is the same at every job count);
- **gem5** (``--gem5``, plan step 4) and the **bench**'s per-task floor (``waveflow.cpu.bench``).

Each timed fidelity runs at several sizes and is fitted to ``setup + n * per_unit``; the best of
``--reps`` runs per point, after waiting for an idle machine (load average below ``--max-load``).
Every run is checked: the counters equal the trace's, untimed and timed charge the same cycles, every
job of the system is correct.  Results go to ``runtime.json`` beside this file, one entry per
workload; each fidelity carries the commit and load it was measured at.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import platform
import subprocess
import time
from collections.abc import Callable
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import Any

import numpy as np
import simpy

from examples.cpu_sched.cpu_sched import run
from examples.cpu_sched.replay import replayable
from waveflow.cpu.bench import SchedTrace, repeat_trace, run_bench
from waveflow.cpu.platform import CpuPlatform

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
RUNTIME = HERE / "runtime.json"
TRACE_JOBS = 200
COPIES = (1, 2, 4)
SYSTEM_JOBS = (100, 200, 400)
SYSTEM_INTERARRIVAL_S = 0.8e-6
#: The bench's sizes: plan step 3 (stream 100,000, burst 20,000), as plans/cpu_model.md measured.
BENCH = {"stream": 100_000, "burst": 20_000}
#: The relative spread the work per operation (and per job) may have across sizes.
FLAT = 0.10


# --- the machine ------------------------------------------------------------------------------


def _git(*args: str) -> str:
    r = subprocess.run(
        ["git", "-C", str(REPO), *args], capture_output=True, text=True, check=False
    )
    return r.stdout.strip()


#: The load average ``wait_idle`` last saw before a series started (``None`` before the first).
_IDLE: dict[str, float | None] = {"load": None}


def _code_dirty() -> bool:
    """Uncommitted changes to tracked files that can change a measurement: not the results this
    script writes, and not Markdown."""
    lines = _git("status", "--porcelain", "--untracked-files=no").splitlines()
    return any(not line.rstrip().endswith(("runtime.json", ".md")) for line in lines)


def stamp() -> dict[str, Any]:
    """When, at which commit and under what load a measurement was taken."""
    return {
        "date": datetime.now(timezone.utc).astimezone().date().isoformat(),
        "commit": _git("rev-parse", "--short", "HEAD"),
        "dirty": _code_dirty(),
        "load": round(os.getloadavg()[0], 2),
        "load_before": _IDLE["load"],
    }


def machine() -> dict[str, Any]:
    cpu = platform.processor()
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                cpu = line.split(":", 1)[1].strip()
                break
    except OSError:
        pass
    return {
        "cpu": cpu,
        "threads": os.cpu_count(),
        "system": platform.platform(),
        "python": platform.python_version(),
        "simpy": simpy.__version__,
        "numpy": np.__version__,
    }


def wait_idle(max_load: float, timeout_s: float = 900.0) -> float:
    """Wait until the 1-minute load average is below *max_load*; give up after *timeout_s*."""
    end = time.monotonic() + timeout_s
    while True:
        load = os.getloadavg()[0]
        if load < max_load:
            _IDLE["load"] = round(load, 2)
            return load
        if time.monotonic() > end:
            raise SystemExit(
                f"the machine stayed busy (load {load:.2f} >= {max_load}) for "
                f"{timeout_s:.0f} s: not measuring"
            )
        print(f"  waiting for an idle machine (load {load:.2f})", flush=True)
        time.sleep(30)


# --- fits and checks -------------------------------------------------------------------------


def _fit(sizes: list[int], secs: list[float], unit: str) -> dict[str, Any]:
    """``secs ~ setup + size * per_unit``, least squares, in milliseconds."""
    per, setup = np.polyfit(np.asarray(sizes, float), np.asarray(secs, float), 1)
    return {
        f"per_{unit}_ms": 1e3 * per,
        "setup_ms": 1e3 * setup,
        "runs": {str(n): s for n, s in zip(sizes, secs)},
    }


#: A run shorter than this is at the mercy of anything else on the machine: it is repeated until
#: ``SHORT_REPS`` runs, still keeping the best.  (With best of 3, one 5 ms point read 5.47 ms against
#: 4.64 ms an hour earlier, and moved the untimed fit by 26 %: plans/sim_speed_cpu_linalg.md step 12.)
SHORT_S = 0.05
SHORT_REPS = 15


def _best(fn: Callable[[], Any], reps: int) -> tuple[float, Any]:
    best, out, n = float("inf"), None, 0
    while n < reps or (best < SHORT_S and n < SHORT_REPS):
        t = time.perf_counter()
        out = fn()
        best = min(best, time.perf_counter() - t)
        n += 1
    return best, out


def _flat(values: list[float]) -> bool:
    return max(values) - min(values) <= FLAT * float(np.mean(values))


def work_per_op(trace: list[dict[str, Any]]) -> dict[str, float]:
    """The work an operation does on average: list entries scanned and moved, and the list's length."""
    return {
        "scanned_moved": float(np.mean([t["n_scanned"] + t["n_moved"] for t in trace])),
        "n_tasks": float(np.mean([t["n_tasks"] for t in trace])),
    }


# --- the fidelities ----------------------------------------------------------------------------


def base_trace() -> list[dict[str, Any]]:
    """The micro-scheduler's trace at its defaults, without the aging calls that found nothing."""
    return replayable(run(TRACE_JOBS, 2).trace)


def time_trace(
    base: list[dict[str, Any]], copies: list[int], platform_: CpuPlatform, reps: int
) -> dict[str, Any]:
    """Untimed and pysim per trace operation, fitted over *copies* repetitions of *base*.

    Each fidelity runs on its own :class:`SchedTrace`, so each prices with its own memo: neither
    warms the other, and the untimed sum and the cycles the processor charges are two independent
    pricings.  The best of several runs is the warm cost (the memo filled by the first run); the
    cold first pass is :func:`time_cold`'s.
    """
    sizes, untimed_s, pysim_s, work = [], [], [], []
    exact = True
    for k in copies:
        trace = repeat_trace(base, k)
        st_u, st_p = SchedTrace(trace, platform_), SchedTrace(trace, platform_)
        sizes.append(len(trace))
        work.append(work_per_op(trace)["scanned_moved"])
        dt_u, u = _best(st_u.untimed, reps)
        exact &= st_u.counters_match() and st_u.ready == []
        dt_p, cpu = _best(st_p.pysim, reps)
        charged = sum(r.cycles for r in cpu.records)
        exact &= (
            st_p.counters_match()
            and st_p.ready == []
            and len(cpu.records) == u["calls"]
        )
        exact &= abs(charged - u["cycles"]) <= 1e-9 * u["cycles"]
        untimed_s.append(dt_u)
        pysim_s.append(dt_p)
        print(
            f"  k={k}: {sizes[-1]} ops  untimed {1e3 * dt_u:7.2f} ms  pysim {1e3 * dt_p:7.2f} ms  "
            f"exact={exact}",
            flush=True,
        )
    if not _flat(work):
        raise RuntimeError(f"the work per operation is not flat across copies: {work}")
    return {
        "untimed": {**_fit(sizes, untimed_s, "op"), "exact": exact, **stamp()},
        "pysim_trace": {**_fit(sizes, pysim_s, "op"), "exact": exact, **stamp()},
        "work_per_op": {str(k): w for k, w in zip(copies, work)},
        "ops": {str(k): n for k, n in zip(copies, sizes)},
    }


def time_cold(
    base: list[dict[str, Any]], platform_: CpuPlatform, reps: int
) -> dict[str, Any]:
    """The first pass over one trace, before any price is memoized: untimed and pysim per operation.

    Each run gets a fresh :class:`SchedTrace` (fresh functions, empty memos); the best of
    ``max(reps, SHORT_REPS)`` fresh runs.  ``points`` is how many distinct prices the trace needs,
    out of ``calls``.
    """
    best_u = best_p = float("inf")
    points = calls = 0
    for _ in range(max(reps, SHORT_REPS)):
        st = SchedTrace(base, platform_)
        t = time.perf_counter()
        u = st.untimed()
        best_u = min(best_u, time.perf_counter() - t)
        points = sum(len(f._prices) for f in (*st.funcs.values(), st.dispatch))
        calls = u["calls"]
        st = SchedTrace(base, platform_)
        t = time.perf_counter()
        st.pysim()
        best_p = min(best_p, time.perf_counter() - t)
    n = len(base)
    print(f"  cold first pass: untimed {1e6 * best_u / n:.2f} us/op  pysim "
          f"{1e6 * best_p / n:.2f} us/op  ({points} distinct prices of {calls} calls)", flush=True)  # fmt: skip
    return {"untimed_us_op": 1e6 * best_u / n, "pysim_us_op": 1e6 * best_p / n, "ops": n,
            "points": points, "calls": calls, **stamp()}  # fmt: skip


def time_replay_pricing() -> dict[str, Any]:
    """What ``examples/cpu_sched/replay.py`` reports for pricing the committed 410-operation trace
    through a ``Processor`` (``_model_wall_s``, best of 15): the figure the example's page quotes.
    """
    from examples.cpu_sched.replay import _model_wall_s, load_trace

    plat = CpuPlatform.load()
    return {
        "ms": 1e3 * _model_wall_s(plat, replayable(load_trace()), repeats=15),
        **stamp(),
    }


def time_system(
    jobs: list[int], interarrival_s: float, platform_: CpuPlatform, reps: int
) -> dict[str, Any]:
    """The whole example per job, fitted over *jobs*."""
    secs, work, ops_per_job = [], [], []
    correct = True
    for n in jobs:
        dt, res = _best(
            partial(run, n, 2, mean_interarrival_s=interarrival_s, platform=platform_),
            reps,
        )
        correct &= res.all_correct and len(res.jobs) == n
        trace = replayable(res.trace)
        work.append(work_per_op(trace)["scanned_moved"])
        ops_per_job.append(len(trace) / n)
        secs.append(dt)
        print(
            f"  N={n}: {1e3 * dt:7.1f} ms  ops/job {ops_per_job[-1]:.2f}  correct={correct}",
            flush=True,
        )
    if not (_flat(work) and _flat(ops_per_job)):
        raise RuntimeError(
            f"the work per job is not flat across N: work/op {work}, "
            f"ops/job {ops_per_job}"
        )
    return {
        **_fit(jobs, secs, "job"),
        "all_correct": correct,
        "interarrival_s": interarrival_s,
        "work_per_op": {str(n): w for n, w in zip(jobs, work)},
        "ops_per_job": {str(n): r for n, r in zip(jobs, ops_per_job)},
        **stamp(),
    }


def bench_rows() -> dict[str, Any]:
    """The bench's tasks per wall second, both arrival patterns (4 cores, as plans/cpu_model.md)."""
    out = {}
    for pattern, n in BENCH.items():
        r = run_bench(pattern, n)
        out[pattern] = {
            k: r[k] for k in ("tasks", "cores", "sim_s", "wall_s", "tasks_per_s")
        }
        print(f"  bench {pattern}: {r['tasks_per_s']:,.0f} tasks/s", flush=True)
    return {**out, **stamp()}


def count_events(fn: Callable[[], Any]) -> collections.Counter:
    """The SimPy events *fn* schedules, by type (patching ``Environment.schedule``: counting slows
    the run, so it is never the timed one)."""
    from simpy import core

    kinds: collections.Counter = collections.Counter()
    orig = core.Environment.schedule

    def schedule(self, event, priority=1, delay=0):
        kinds[type(event).__name__] += 1
        return orig(self, event, priority, delay)

    core.Environment.schedule = schedule  # type: ignore[method-assign]
    try:
        fn()
    finally:
        core.Environment.schedule = orig  # type: ignore[method-assign]
    return kinds


def events(base: list[dict[str, Any]], platform_: CpuPlatform) -> dict[str, Any]:
    """SimPy events per trace operation, per system job and per bench task, by type."""

    def per(kinds: collections.Counter, n: int) -> dict[str, Any]:
        return {
            "total": sum(kinds.values()) / n,
            "by_type": {k: v / n for k, v in kinds.most_common()},
        }

    st = SchedTrace(base, platform_)
    n_sys = SYSTEM_JOBS[1]
    return {
        "per_op": per(count_events(st.pysim), len(base)),
        "per_job": per(
            count_events(
                partial(
                    run,
                    n_sys,
                    2,
                    mean_interarrival_s=SYSTEM_INTERARRIVAL_S,
                    platform=platform_,
                )
            ),
            n_sys,
        ),
        **{
            f"per_task_{p}": per(count_events(partial(run_bench, p, 2000)), 2000)
            for p in BENCH
        },
    }


#: Where a profiled run's time goes: the first area whose path fragment a function's file contains.
AREAS = (
    ("SimPy core", ("/simpy/",)),
    ("Waveflow simulation framework", ("waveflow/simulation/",)),
    (
        "processor model (scheduling, accounting)",
        ("waveflow/cpu/processor.py", "waveflow/cpu/report.py"),
    ),
    (
        "pricing (cost models, features)",
        ("waveflow/cpu/task.py", "waveflow/calib/", "waveflow/cpu/config.py"),
    ),
    (
        "software bodies (list operations, driver)",
        ("waveflow/cpu/calib/kernels/", "waveflow/cpu/bench.py", "examples/cpu_sched/"),
    ),
    (
        "bus, registers, accelerators",
        ("waveflow/hw/", "examples/regmap/", "waveflow/utils/"),
    ),
)
OTHER = "other"


def _area(filename: str) -> str | None:
    """The area of a function by its file; ``None`` for a C built-in or a library's own Python
    (numpy's, the standard library's): their time is charged to the code that called them.
    """
    if filename == "~" or filename.startswith("<"):
        return None
    path = filename.replace("\\", "/")
    for name, parts in AREAS:
        if any(part in path for part in parts):
            return name
    if "/site-packages/" in path or "/lib/python3" in path:
        return None
    return OTHER


def _charged_to(stats: dict, func: tuple, memo: dict, stack: set) -> dict[str, float]:
    """The areas *func*'s own time is charged to, as fractions: its own area, or -- for a built-in
    or a library frame -- its callers' areas, in proportion to the time each call took, up the
    chain (a cycle, or a frame with no caller, counts as ``other``)."""
    if func in memo:
        return memo[func]
    area = _area(func[0])
    if area is not None:
        out = {area: 1.0}
    elif func in stack or func not in stats or not stats[func][4]:
        out = {OTHER: 1.0}
    else:
        weights = {c: (v[2] or v[3] or v[1]) for c, v in stats[func][4].items()}
        total = sum(weights.values())
        out = {}
        stack.add(func)
        for caller, w in weights.items():
            for a, f in _charged_to(stats, caller, memo, stack).items():
                out[a] = out.get(a, 0.0) + f * w / total
        stack.discard(func)
    memo[func] = out
    return out


def profile_run(fn: Callable[[], Any], top: int = 10) -> dict[str, Any]:
    """cProfile *fn*: each area's share of the run's own time, and the top functions by own time.

    A C built-in's or a library's own time (numpy's, the standard library's) goes to the areas of
    the code that called it, in proportion, so "pricing" includes the numpy it runs.  cProfile's overhead is
    larger on call-heavy code (SimPy's), so the shares are indicative; timings never use it.
    """
    import cProfile
    import pstats

    prof = cProfile.Profile()
    prof.enable()
    fn()
    prof.disable()
    stats = pstats.Stats(prof).stats  # type: ignore[attr-defined]
    total = sum(v[2] for v in stats.values())
    shares: collections.Counter = collections.Counter()
    memo: dict = {}
    for func, (_cc, _nc, tt, _ct, _callers) in stats.items():
        for area, frac in _charged_to(stats, func, memo, set()).items():
            shares[area] += tt * frac
    ranked = sorted(stats.items(), key=lambda kv: kv[1][2], reverse=True)[:top]
    return {
        "total_s": total,
        "shares": {k: v / total for k, v in shares.most_common()},
        "top": [
            {
                "function": f"{Path(f).name}:{line}({name})",
                "own_share": v[2] / total,
                "calls": v[1],
            }
            for (f, line, name), v in ranked
        ],
    }


def profiles(
    base: list[dict[str, Any]],
    copies: list[int],
    jobs: list[int],
    interarrival_s: float,
    platform_: CpuPlatform,
) -> dict[str, Any]:
    """Where the time goes: the untimed sum and the pysim at the largest trace, the system at the
    most jobs."""
    st = SchedTrace(repeat_trace(base, copies[-1]), platform_)
    out = {
        "untimed": profile_run(st.untimed),
        "pysim_trace": profile_run(st.pysim),
        "pysim_system": profile_run(
            partial(
                run, jobs[-1], 2, mean_interarrival_s=interarrival_s, platform=platform_
            )
        ),
    }
    for name, p in out.items():
        print(
            f"  profile {name}: "
            + ", ".join(
                f"{k} {100 * v:.0f}%" for k, v in list(p["shares"].items())[:4]
            ),
            flush=True,
        )
    return {**out, "trace_copies": copies[-1], "system_jobs": jobs[-1], **stamp()}


def time_gem5(
    base: list[dict[str, Any]], copies: list[int], reps: int
) -> dict[str, Any]:
    """The same traces on gem5's HPI core (``sched_replay.c``, ``mode=total``), per operation.

    Two costs, fitted apart.  The **region** is the measured pass alone: the host time gem5 spent
    simulating it (block 0's ``simTicks / hostTickRate``, and the printed ``hostSeconds`` at 0.01 s
    resolution beside it).  The **wall** time is end to end: the container's start, parsing the trace,
    a warm-up pass over it, the measured pass, and printing three counters per operation.  The build
    is timed once, in a fresh work directory (the runner caches binaries per directory).
    """
    from examples.cpu_sched.replay import (
        CYCLES,
        REPLAY,
        _expected,
        load_trace,
        replay_total,
        trace_text,
    )
    from waveflow.cpu.calib.gem5 import GEM5_TAG, Gem5Runner, stat_series

    workdir = REPO / "work" / "sim_speed" / f"gem5_{int(time.time())}"
    runner = Gem5Runner(workdir=workdir)
    why = runner.unavailable()
    if why:
        raise SystemExit(f"gem5 is not available: {why}")
    t = time.perf_counter()
    runner.build(REPLAY)
    build_s = time.perf_counter() - t

    sizes: list[int] = []
    wall: list[float] = []
    host: list[float] = []
    host_printed: list[float] = []
    cycles: dict[str, float] = {}
    model: dict[str, float] = {}
    match = True
    for k in copies:
        trace = repeat_trace(base, k)
        files = {"trace.txt": trace_text(trace)}
        best: dict[str, float] = {}
        for _ in range(reps):
            t = time.perf_counter()
            out, _, rundir = runner.run(
                REPLAY,
                {"trace": "/run/trace.txt", "mode": "total"},
                extra_files=files,
                compact_stats=True,
                parse=False,
            )
            dt = time.perf_counter() - t
            stats = rundir / "m5out" / "stats.txt"
            region_cycles = stat_series(stats, CYCLES)[0]
            region_s = (
                stat_series(stats, "simTicks")[0]
                / stat_series(stats, "hostTickRate")[0]
            )
            match &= out == _expected(trace)
            if str(k) in cycles and cycles[str(k)] != region_cycles:
                raise RuntimeError(f"gem5 is not deterministic at k={k}")
            cycles[str(k)] = region_cycles
            for name, v in (
                ("wall", dt),
                ("host", region_s),
                ("printed", stat_series(stats, "hostSeconds")[0]),
            ):
                best[name] = min(best.get(name, v), v)
        sizes.append(len(trace))
        wall.append(best["wall"])
        host.append(best["host"])
        host_printed.append(best["printed"])
        model[str(k)] = SchedTrace(trace).untimed()["cycles"]
        print(
            f"  k={k}: {len(trace)} ops  region {best['host']:6.2f} s host, "
            f"{region_cycles:,.0f} cycles  wall {best['wall']:6.1f} s  counters={match}",
            flush=True,
        )
    anchor = replay_total(load_trace(), runner=runner)
    return {
        **_fit(sizes, host, "op"),
        "counters_match": match,
        "region_host_printed": _fit(sizes, host_printed, "op"),
        "wall": _fit(sizes, wall, "op"),
        "region_cycles": cycles,
        "model_cycles": model,
        "anchor": {
            "trace": "tests/fixtures/cpu/cpu_sched_trace.jsonl",
            **anchor,
            "pinned": 63266.0,
            "ok": anchor["counters_match"] and anchor["total_cycles"] == 63266.0,
        },
        "build_s": build_s,
        "gem5_tag": GEM5_TAG,
        "image": runner.image,
        "reps": reps,
        **stamp(),
    }


# --- the driver ----------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--copies", type=int, nargs="+", default=list(COPIES))
    ap.add_argument(
        "--jobs",
        type=int,
        nargs="+",
        default=list(SYSTEM_JOBS),
        help="job counts of the system row",
    )
    ap.add_argument("--system-interarrival", type=float, default=SYSTEM_INTERARRIVAL_S)
    ap.add_argument(
        "--reps", type=int, default=3, help="best of this many runs per point"
    )
    ap.add_argument(
        "--max-load",
        type=float,
        default=1.0,
        help="wait for a load average below this before each series",
    )
    ap.add_argument("--no-bench", action="store_true", help="skip the bench rows")
    ap.add_argument(
        "--gem5", action="store_true", help="also replay the traces on gem5 (Docker)"
    )
    ap.add_argument(
        "--profile", action="store_true", help="also profile where the time goes"
    )
    a = ap.parse_args(argv)
    copies, jobs = sorted(a.copies), sorted(a.jobs)

    plat = CpuPlatform.load()
    base = base_trace()
    key = f"sched{TRACE_JOBS}_x{''.join(map(str, copies))}"
    data = json.loads(RUNTIME.read_text(encoding="utf-8")) if RUNTIME.exists() else {}
    entry: dict[str, Any] = data.get(key, {})
    entry["meta"] = {
        "machine": machine(),
        "platform": plat.name,
        "reps": a.reps,
        "trace": {
            "source": f"examples.cpu_sched.cpu_sched.run({TRACE_JOBS}, 2), replayable",
            "ops": len(base),
            "work_per_op": work_per_op(base),
        },
        "copies": copies,
        "system_jobs": jobs,
        **stamp(),
    }
    print(f"trace: {len(base)} ops, copies {copies}", flush=True)

    wait_idle(a.max_load)
    entry.update(time_trace(base, copies, plat, a.reps))
    entry["cold"] = time_cold(base, plat, a.reps)
    entry["replay_pricing"] = time_replay_pricing()
    print(f"untimed      {entry['untimed']['per_op_ms'] * 1e3:8.2f} us/op", flush=True)
    print(
        f"pysim trace  {entry['pysim_trace']['per_op_ms'] * 1e3:8.2f} us/op  "
        f"setup {entry['pysim_trace']['setup_ms']:.2f} ms",
        flush=True,
    )

    wait_idle(a.max_load)
    entry["pysim_system"] = time_system(jobs, a.system_interarrival, plat, a.reps)
    print(
        f"pysim system {entry['pysim_system']['per_job_ms']:8.3f} ms/job  "
        f"setup {entry['pysim_system']['setup_ms']:.1f} ms",
        flush=True,
    )

    if not a.no_bench:
        wait_idle(a.max_load)
        entry["bench"] = bench_rows()

    entry["events"] = {**events(base, plat), **stamp()}
    print(
        f"events       {entry['events']['per_op']['total']:.2f}/op  "
        f"{entry['events']['per_job']['total']:.2f}/job",
        flush=True,
    )
    if a.profile:
        entry["profile"] = profiles(base, copies, jobs, a.system_interarrival, plat)
    entry.setdefault("history", [])

    if a.gem5:
        wait_idle(a.max_load)
        entry["gem5"] = time_gem5(base, copies, a.reps)
        g = entry["gem5"]
        print(
            f"gem5 region  {g['per_op_ms'] * 1e3:8.1f} us/op  wall {g['wall']['per_op_ms'] * 1e3:.1f} "
            f"us/op (setup {g['wall']['setup_ms'] / 1e3:.1f} s)  build {g['build_s']:.1f} s  "
            f"anchor ok={g['anchor']['ok']}",
            flush=True,
        )

    data[key] = entry
    RUNTIME.write_text(
        json.dumps(dict(sorted(data.items())), indent=1) + "\n", encoding="utf-8"
    )

    ok = (
        entry["untimed"]["exact"]
        and entry["pysim_trace"]["exact"]
        and entry["pysim_system"]["all_correct"]
        and (
            not a.gem5
            or (entry["gem5"]["counters_match"] and entry["gem5"]["anchor"]["ok"])
        )
    )
    print(f"-> {RUNTIME.relative_to(REPO)} [{key}]  exact={ok}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
