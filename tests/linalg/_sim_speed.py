"""What a job of the systolic unit and of the CG vector unit costs: the untimed model, the pysim, the RTL.

``docs/evaluation/sim_speed/linalg.md`` is written from what this records
(``plans/sim_speed_cpu_linalg.md``, steps 6-9).  Each unit runs in its own memory-fed bench
(``tests/linalg/_unit_bench.py``, ``tests/linalg/_cg_unit_bench.py``) at the centre configuration of
its tests (``UNITS["centre"]``, imported from ``test_systolic_unit`` and ``test_cg_vector_unit``), on
N identical full-size jobs:

- **systolic** -- ``MUL`` of ``Mmax x Kmax`` by ``Kmax x Nmax`` (8 x 8 by 8 x 32), one request a job;
- **cg** -- ``nitmax`` iterations on ``Kmax x Kmax`` and ``Kmax x Nmax`` (8 x 8, 8 x 32): a ``START``
  and eight ``STEP`` requests a job, each answered, ``S`` from the model.

This module holds the workloads and the run helpers; the outcome test
(``tests/linalg/test_sim_speed_outcomes.py``) pins what the pysim does with them.
"""

from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path
from typing import Any

import numpy as np

from tests.linalg import _cg_unit_bench as CB
from tests.linalg import _unit_bench as UB
from waveflow.linalg import cg
from waveflow.linalg import matmul as mm
from waveflow.linalg.systolic import MatmulOp

BLOCKS = ("systolic", "cg")


def unit_config(block: str) -> dict[str, Any]:
    """The centre configuration of *block*'s unit tests."""
    if block == "systolic":
        from tests.linalg.test_systolic_unit import UNITS
    elif block == "cg":
        from tests.linalg.test_cg_vector_unit import UNITS  # type: ignore[no-redef]
    else:
        raise ValueError(f"unknown block {block!r}")
    return dict(UNITS["centre"])


def make_jobs(block: str, n: int, seed: int = 1) -> list[Any]:
    """*n* full-size jobs of *block*, drawn from *seed*."""
    p = unit_config(block)
    rng = np.random.default_rng([seed, n])
    if block == "systolic":
        m, k, nn = int(p["Mmax"]), int(p["Kmax"]), int(p["Nmax"])
        return [UB.random_job(rng, p, MatmulOp.MUL, m, k, nn) for _ in range(n)]
    f, k, nn, nit = p["formats"], int(p["Kmax"]), int(p["Nmax"]), int(p["nitmax"])
    return [CB.random_job(rng, f, nit, k, nn) for _ in range(n)]


def replies_per_job(block: str) -> int:
    return 1 if block == "systolic" else 1 + int(unit_config(block)["nitmax"])


def make_sim(block: str, jobs: list[Any], n_cycles: int | None = None) -> Any:
    """The bench of *block* around *jobs*.  Building it computes the golden (the CG bench's
    expected replies), so it is never inside a pysim timer."""
    p = unit_config(block)
    kw = {} if n_cycles is None else {"n_cycles": int(n_cycles)}
    if block == "systolic":
        return UB.UnitBenchSim(p, jobs, **kw)
    return CB.CgUnitBenchSim(p, jobs, **kw)


def stamp_replies(sim: Any) -> list[float]:
    """Record the simulated time each reply header reaches the bench's ``s_done`` sink.

    The sink's ``rx_proc`` is read on every burst (``waveflow/hw/interface.py``), so wrapping it
    before the run sees every reply, without a parameter on the bench.
    """
    ep = sim.tb.done_sink.stream_ep
    env = sim.tb.sim.env
    orig = ep.rx_proc
    times: list[float] = []

    def stamped(words):
        times.append(float(env.now))
        yield from orig(words)

    ep.rx_proc = stamped
    return times


def run_checked(sim: Any, root: Path) -> tuple[dict, bool]:
    """Write the scenario under *root*, run the pysim, and check every reply as the bench's own
    ``run()`` does -- returning the verdict instead of raising."""
    sc = sim.write_scenario(root)
    sim.tb.sim.run_sim()
    return sc, check(sim, sc)


def check(sim: Any, sc: dict) -> bool:
    bpw = sim.w // 8
    for off, exp in sc["expected"]:
        got = sim.tb.mem._mem.read(off * bpw, len(exp)).astype(np.uint64)
        if not np.array_equal(got, exp):
            return False
    done = [np.asarray(b, np.uint64) for b in sim.tb.done_sink.words]
    return len(done) == len(sc["done"]) and all(
        np.array_equal(g, w) for g, w in zip(done, sc["done"])
    )


def outcome(block: str, n: int, seed: int = 1) -> dict[str, Any]:
    """What the pysim of *n* jobs does: every reply's arrival time, a digest of every reply's words
    (headers and payloads, read back from the bench's memory), the end time -- and whether it was
    bit-exact."""
    sim = make_sim(block, make_jobs(block, n, seed))
    times = stamp_replies(sim)
    with tempfile.TemporaryDirectory() as root:
        sc, exact = run_checked(sim, Path(root))
    h = hashlib.sha256()
    for words in sim.tb.done_sink.words:
        h.update(np.asarray(words, np.uint64).tobytes())
    bpw = sim.w // 8
    for off, exp in sc["expected"]:
        h.update(sim.tb.mem._mem.read(off * bpw, len(exp)).astype(np.uint64).tobytes())
    return {
        "jobs": n,
        "replies": len(times),
        "arrivals": times,
        "words_digest": h.hexdigest(),
        "end": float(sim.tb.sim.env.now),
        "exact": exact,
    }


def untimed(block: str, jobs: list[Any]) -> list[Any]:
    """The arithmetic of *jobs* by the bit-exact model alone: per systolic job one ``matmul``, per CG
    job ``cg_init`` and ``nit`` ``vec_step`` (its ``S`` matrices given, as the unit is given them).

    The CG ``S`` stimulus is computed by :func:`cg_stimulus` beforehand, not here.
    """
    p = unit_config(block)
    out: list[Any] = []
    if block == "systolic":
        for j in jobs:
            (ar, ai), (br, bi) = j.a, j.b
            out.append(
                mm.matmul(
                    ar,
                    ai,
                    p["a"],
                    br,
                    bi,
                    p["b"],
                    p["c"],
                    adjoint=j.op == MatmulOp.MUL_AH,
                    form=int(p["form"]),
                )
            )
        return out
    f = p["formats"]
    for job, ss in jobs:
        br, bi = job.b
        state = cg.cg_init(br, bi, f)
        for sr, si in ss:
            state, _ = cg.vec_step(state, sr, si, f)
        out.append((state.xr, state.xi))
    return out


def cg_stimulus(jobs: list[Any]) -> list[tuple[Any, list]]:
    """Each CG job with its ``S`` matrices (the model's), the stimulus :func:`untimed` takes."""
    f = unit_config("cg")["formats"]
    return [(job, CB.run_job(job, f)[0]) for job in jobs]


# --- the timing driver (plan step 7) --------------------------------------------------------------
#
#     python -m tests.linalg._sim_speed                    # untimed, pysim, construction, events
#     python -m tests.linalg._sim_speed --block systolic   # one unit
#
# Each fidelity runs at ``--jobs`` sizes and is fitted to ``setup + jobs * per_job``; the best of
# ``--reps`` runs per point, after waiting for an idle machine.  The pysim timer wraps ``run_sim()``
# alone: building the bench and writing its scenario compute the golden, so they are timed apart as
# ``construction``.  The helpers below are copies of ``examples/cpu_sched/cpu_sched_runtime.py``'s:
# ``tests/linalg`` must not import ``examples`` (``test_no_example_imports.py``).

RUNTIME = Path(__file__).resolve().parent / "sim_speed_runtime.json"
REPO = Path(__file__).resolve().parents[2]
JOBS = (10, 50, 100)


def _git(*args: str) -> str:
    import subprocess

    r = subprocess.run(
        ["git", "-C", str(REPO), *args], capture_output=True, text=True, check=False
    )
    return r.stdout.strip()


def stamp() -> dict[str, Any]:
    """When, at which commit and under what load a measurement was taken."""
    import os
    from datetime import datetime, timezone

    return {
        "date": datetime.now(timezone.utc).astimezone().date().isoformat(),
        "commit": _git("rev-parse", "--short", "HEAD"),
        "dirty": bool(_git("status", "--porcelain", "--untracked-files=no")),
        "load": round(os.getloadavg()[0], 2),
    }


def machine() -> dict[str, Any]:
    import os
    import platform

    import simpy

    cpu = platform.processor()
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                cpu = line.split(":", 1)[1].strip()
                break
    except OSError:
        pass
    return {"cpu": cpu, "threads": os.cpu_count(), "system": platform.platform(),
            "python": platform.python_version(), "simpy": simpy.__version__,
            "numpy": np.__version__}  # fmt: skip


def wait_idle(max_load: float, timeout_s: float = 900.0) -> float:
    """Wait until the 1-minute load average is below *max_load*; give up after *timeout_s*."""
    import os
    import time

    end = time.monotonic() + timeout_s
    while True:
        load = os.getloadavg()[0]
        if load < max_load:
            return load
        if time.monotonic() > end:
            raise SystemExit(f"the machine stayed busy (load {load:.2f} >= {max_load}) for "
                             f"{timeout_s:.0f} s: not measuring")  # fmt: skip
        print(f"  waiting for an idle machine (load {load:.2f})", flush=True)
        time.sleep(30)


def _fit(sizes: list[int], secs: list[float]) -> dict[str, Any]:
    """``secs ~ setup + jobs * per_job``, least squares, in milliseconds."""
    per, setup = np.polyfit(np.asarray(sizes, float), np.asarray(secs, float), 1)
    return {"per_job_ms": 1e3 * per, "setup_ms": 1e3 * setup,
            "runs": {str(n): s for n, s in zip(sizes, secs)}}  # fmt: skip


def count_events(fn) -> dict[str, float]:
    """The SimPy events *fn* schedules, by type (never a timed run: counting slows it)."""
    import collections

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
    return dict(kinds.most_common())


def untimed_exact(block: str, sim: Any, sc: dict, out: list[Any]) -> bool:
    """The untimed results are the bench's expected replies, word for word: each systolic job's
    ``C``, each CG job's ``X`` (its last reply)."""
    from waveflow.linalg.lanes import to_words

    p = unit_config(block)
    fmt = p["c"] if block == "systolic" else p["formats"].X
    per = replies_per_job(block)
    for j, (re, im) in enumerate(out):
        want = sc["expected"][(j + 1) * per - 1][1]
        got = to_words(re, im, fmt, int(p["lane_bits"]), int(p["word_bits"]))
        if not np.array_equal(np.asarray(got, np.uint64), np.asarray(want, np.uint64)):
            return False
    return True


def measure(block: str, sizes: list[int], reps: int, seed: int = 1) -> dict[str, Any]:
    """Untimed, pysim and construction per job of *block*, fitted over *sizes*."""
    import time

    t_un, t_py, t_co, ends = [], [], [], []
    exact = True
    for n in sizes:
        jobs = make_jobs(block, n, seed)
        stim = cg_stimulus(jobs) if block == "cg" else jobs
        best_u = best_p = best_c = float("inf")
        for _ in range(reps):
            t = time.perf_counter()
            out = untimed(block, stim)
            best_u = min(best_u, time.perf_counter() - t)
            with tempfile.TemporaryDirectory() as root:
                t = time.perf_counter()
                sim = make_sim(block, jobs)
                sc = sim.write_scenario(Path(root))
                best_c = min(best_c, time.perf_counter() - t)
                t = time.perf_counter()
                sim.tb.sim.run_sim()
                best_p = min(best_p, time.perf_counter() - t)
            exact &= check(sim, sc) and untimed_exact(block, sim, sc, out)
        ends.append(float(sim.tb.sim.env.now))
        t_un.append(best_u)
        t_py.append(best_p)
        t_co.append(best_c)
        print(f"  {block} N={n}: untimed {1e3 * best_u:8.2f} ms  pysim {1e3 * best_p:8.2f} ms  "
              f"construction {1e3 * best_c:8.2f} ms  exact={exact}", flush=True)  # fmt: skip
    st = stamp()
    return {
        "untimed": {**_fit(sizes, t_un), "bits_exact": exact, **st},
        "pysim": {**_fit(sizes, t_py), "bits_exact": exact,
                  "end_s": {str(n): e for n, e in zip(sizes, ends)}, **st},
        "construction": {**_fit(sizes, t_co), **st},
    }  # fmt: skip


def events(block: str, n: int, seed: int = 1) -> dict[str, Any]:
    """SimPy events per job of *block*, by type, from a run of *n* jobs."""
    sim = make_sim(block, make_jobs(block, n, seed))
    with tempfile.TemporaryDirectory() as root:
        sim.write_scenario(Path(root))
        kinds = count_events(sim.tb.sim.run_sim)
    return {"jobs": n, "total": sum(kinds.values()) / n,
            "by_type": {k: v / n for k, v in kinds.items()}, **stamp()}  # fmt: skip


def _config(block: str) -> dict[str, Any]:
    return {k: (str(v) if not isinstance(v, (int, float, str)) else v)
            for k, v in unit_config(block).items()}  # fmt: skip


def main(argv: list[str] | None = None) -> int:
    import argparse
    import json

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--block", nargs="+", default=list(BLOCKS), choices=BLOCKS)
    ap.add_argument("--jobs", type=int, nargs="+", default=list(JOBS))
    ap.add_argument(
        "--reps", type=int, default=3, help="best of this many runs per point"
    )
    ap.add_argument("--max-load", type=float, default=1.0,
                    help="wait for a load average below this before each series")  # fmt: skip
    a = ap.parse_args(argv)
    sizes = sorted(a.jobs)
    data = json.loads(RUNTIME.read_text(encoding="utf-8")) if RUNTIME.exists() else {}
    ok = True
    for block in a.block:
        key = f"{block}_centre"
        entry: dict[str, Any] = data.get(key, {})
        entry["meta"] = {"machine": machine(), "config": _config(block), "reps": a.reps,
                         "jobs": sizes, "replies_per_job": replies_per_job(block),
                         **stamp()}  # fmt: skip
        wait_idle(a.max_load)
        entry.update(measure(block, sizes, a.reps))
        entry["events"] = events(block, sizes[1] if len(sizes) > 1 else sizes[0])
        entry.setdefault("history", [])
        u, py = entry["untimed"], entry["pysim"]
        print(f"{block}: untimed {u['per_job_ms']:.3f} ms/job  pysim {py['per_job_ms']:.3f} ms/job "
              f"({py['per_job_ms'] / u['per_job_ms']:.1f}x)  construction "
              f"{entry['construction']['per_job_ms']:.3f} ms/job  "
              f"events {entry['events']['total']:.1f}/job", flush=True)  # fmt: skip
        ok &= u["bits_exact"] and py["bits_exact"]
        data[key] = entry
        RUNTIME.write_text(json.dumps(dict(sorted(data.items())), indent=1) + "\n",
                           encoding="utf-8")  # fmt: skip
    print(f"-> {RUNTIME.relative_to(REPO)}  exact={ok}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
