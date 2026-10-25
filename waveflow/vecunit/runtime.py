"""runtime.py — what a vector unit's workload costs to simulate: its pysim timed and checked, and its
RTL under XSI timed through the system flow (``plans/vecunit_new.md``, Stage 8).

Application-free: each function takes a unit (an instance) and a workload kind it names.  The
examples' runtime scripts (``examples/vecunit_cg/cg_runtime.py``, ``examples/vecunit_mf/mf_runtime.py``)
choose the sizes and the untimed models and keep the results; ``docs/evaluation/sim_speed/`` is
written from them.  Times are the best of several runs: other load on a machine only ever adds time.

- :func:`time_pysim` -- the unit in its harness: building the harness (the stimulus), writing the
  scenario and the run, timed whole and apart, with the SimPy events and the simulated cycles; every
  output burst checked against the interpreter's after the clock stops.
- :func:`time_rtl` -- the harness through csynth and XSI (:func:`waveflow.vecunit.build.build_dag`):
  each step's time, the XSI runner's phases, the cycle the last output left.  The harness has no host
  to end the RTL run, so it simulates a fixed bound (:func:`rtl_bound`); ``rtl_s`` charges it only for
  the cycles the work took, ``simulate × done ÷ bound``.
- :func:`profile_run` -- where one pysim run's time goes: the interpreter, the word conversion, and the
  rest (SimPy and the framework).
"""

from __future__ import annotations

import cProfile
import json
import platform
import pstats
import tempfile
import time
from pathlib import Path

import numpy as np

#: The bound for pysim's own run: past any workload's length (it ends when no event is left).
PYSIM_BOUND = 10**8

#: What :func:`profile_run` charges to the interpreter and to the word conversion: ``(file, function)``
#: entry points the tasks call (none calls another of the set, so their times add).
INTERPRETER = {("model.py", f) for f in ("run_pass", "scalar", "control", "exec_items",
                                          "program_prefix")}
CONVERSION = {("lanes.py", "to_words"), ("lanes.py", "from_words"),
              ("model.py", "block_from_matrix"), ("model.py", "matrix_from_block")}


def best_of(fn, reps: int) -> float:
    """Seconds of the fastest of *reps* calls of *fn*."""
    best = float("inf")
    for _ in range(reps):
        t = time.perf_counter()
        fn()
        best = min(best, time.perf_counter() - t)
    return best


def fit(counts, secs) -> dict:
    """``secs ~ setup + counts * per``, least squares, in milliseconds."""
    per, setup = np.polyfit(np.asarray(counts, float), np.asarray(secs, float), 1)
    return {"per_ms": 1e3 * float(per), "setup_ms": 1e3 * float(setup)}


def outputs_match(sysm, golden: dict) -> bool:
    """Whether every sink of *sysm* (a harness after its run) holds *golden*'s bursts, word for word."""
    for port, bursts in golden.items():
        got = sysm.stand_ins[port].words
        if len(got) != len(bursts):
            return False
        if not all(np.array_equal(np.asarray(a, np.uint64).reshape(-1), np.asarray(b, np.uint64))
                   for a, b in zip(got, bursts)):
            return False
    return True


def time_pysim(unit, kind: str, reps: int) -> dict:
    """*unit* in its harness on workload *kind*, *reps* times.  ``s``: the fastest whole run (build the
    harness, write the scenario, run); ``build_s`` and ``run_s``: the fastest of each part; the SimPy
    events and the simulated cycles (the last event's time); ``exact``: every run's outputs equal the
    interpreter's (:meth:`~waveflow.vecunit.unit.VecUnit.golden`, computed once, outside the clock)."""
    from waveflow.hw.harness import harness

    golden = unit.golden(kind)
    best = build_best = run_best = None
    cycles, events, exact = 0.0, 0, True
    for _ in range(reps):
        with tempfile.TemporaryDirectory() as tmp:
            t0 = time.perf_counter()
            sysm = harness(unit, workload=kind, n_cycles=PYSIM_BOUND)
            sysm.write_scenario(Path(tmp))
            t1 = time.perf_counter()
            sysm.run()
            t2 = time.perf_counter()
        best = min(best or 1e9, t2 - t0)
        build_best = min(build_best or 1e9, t1 - t0)
        run_best = min(run_best or 1e9, t2 - t1)
        cycles, events = sysm.sim.env.now / sysm.clock().period, next(sysm.sim.env._eid)
        exact = exact and outputs_match(sysm, golden)
    return {"s": best, "build_s": build_best, "run_s": run_best, "cycles": cycles,
            "events": events, "exact": exact}


def rtl_bound(pysim_cycles: float) -> int:
    """The RTL's fixed run: the harness has no host to end it, so it simulates every cycle of its
    bound -- set a little past the pysim's length, not far past it."""
    return int(1.2 * pysim_cycles) + 2000


def _last_cycles(vectors: Path) -> dict[str, int]:
    """The cycle of the last word of each recording under *vectors* (an output's, or an input's
    acceptance)."""
    out = {}
    for d in sorted(vectors.iterdir()) if vectors.is_dir() else ():
        f = d / "cycles.bin"
        if f.is_file():
            c = np.fromfile(f, dtype="<u8")
            if c.size:
                out[d.name] = int(c[-1])
    return out


def time_rtl(unit, kind: str, root: Path, n_cycles: int) -> dict:
    """*unit*'s harness on workload *kind* through the system flow at *root*, the RTL bounded at
    *n_cycles*: each step's time (csynth is fresh only on a root's first run), the XSI runner's phases,
    whether every step passed (``compare``: the RTL's outputs equal pysim's), the cycle each output's
    last word left, the status cycles, and the simulation's time charged for the work alone."""
    from waveflow import events
    from waveflow.hw.harness import ACCEPT_SUFFIX
    from waveflow.vecunit.build import TOP, WORK_DIR, build_config, build_dag

    dag = build_dag(unit, workload=kind, n_cycles=n_cycles)
    config = build_config(root)
    with events.collect() as spans:
        results = dag.run(config, through="compare")
    phases: dict[str, float] = {}
    for sp in spans:
        if sp.get("kind") == "phase":
            phases[sp["name"]] = phases.get(sp["name"], 0.0) + float(sp.get("elapsed", 0.0))
    steps = {k: r.elapsed_seconds for k, r in results.items()}
    bad = [k for k, r in results.items() if not r.success]
    vectors = Path(root) / WORK_DIR / TOP / "xsi_run" / "vectors"
    last = _last_cycles(vectors)
    status = vectors / "m_status" / "cycles.bin"
    sim_s = phases.get("simulate", steps.get("system_xsi", 0.0))
    done = max((c for name, c in last.items() if not name.endswith(ACCEPT_SUFFIX)), default=None)
    return {"steps_s": steps, "phases_s": phases, "exact": not bad,
            "failed": {k: results[k].message[:500] for k in bad} or None,
            "tool": config.vitis_version, "bound_cycles": n_cycles, "simulate_s": sim_s,
            "done_cycle": done, "last_cycles": last,
            "status_cycles": np.fromfile(status, dtype="<u8").tolist() if status.is_file() else [],
            "cycles_per_s": n_cycles / sim_s if sim_s else None,
            "rtl_s": sim_s * done / n_cycles if sim_s and done else None}


def _split(stats: pstats.Stats, entries: set) -> float:
    """Cumulative seconds in the functions *entries* names, ``(file name, function)``."""
    table = stats.stats  # type: ignore[attr-defined]  # pstats keeps it undeclared
    return sum(ct for (path, _line, fn), (_cc, _nc, _tt, ct, _callers) in table.items()
               if (Path(path).name, fn) in entries)


def profile_run(unit, kind: str) -> dict:
    """One pysim run of *unit* on *kind* under cProfile: the harness's build and the run, and the run
    split into the interpreter (:data:`INTERPRETER`), the word conversion (:data:`CONVERSION`) and the
    rest -- SimPy, the interfaces, the tasks' own code.  cProfile's overhead inflates every call, so the
    shares are what to read, not the seconds."""
    from waveflow.hw.harness import harness

    with tempfile.TemporaryDirectory() as tmp:
        build, run = cProfile.Profile(), cProfile.Profile()
        build.enable()
        sysm = harness(unit, workload=kind, n_cycles=PYSIM_BOUND)
        sysm.write_scenario(Path(tmp))
        build.disable()
        run.enable()
        sysm.run()
        run.disable()
    bs, rs = pstats.Stats(build), pstats.Stats(run)
    total, build_total = rs.total_tt, bs.total_tt  # type: ignore[attr-defined]
    interp, conv = _split(rs, INTERPRETER), _split(rs, CONVERSION)
    return {"build_s": build_total, "build_interpreter_s": _split(bs, {("model.py", "run_job")}),
            "run_s": total, "interpreter_s": interp, "conversion_s": conv,
            "rest_s": total - interp - conv, "events": next(sysm.sim.env._eid)}


def machine() -> dict:
    """The machine a measurement ran on (no host name)."""
    cpu = platform.processor()
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                cpu = line.split(":", 1)[1].strip()
                break
    except OSError:
        pass
    import os

    return {"cpu": cpu, "threads": os.cpu_count(), "python": platform.python_version(),
            "system": platform.platform()}


def load(path: Path, key: str) -> dict:
    """The record filed under *key* in the JSON at *path* (empty if none)."""
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    return data.get(key, {})


def keep_previous(path: Path, key: str, out: dict) -> None:
    """Carry into *out* what this run did not repeat from the record at *path*: each size's ``rtl``
    row (a run without ``--rtl``) and the profile."""
    prev = load(path, key)
    for size, row in prev.get("sizes", {}).items():
        if "rtl" in row and size in out.get("sizes", {}) and "rtl" not in out["sizes"][size]:
            out["sizes"][size]["rtl"] = row["rtl"]
    if "profile" in prev and "profile" not in out:
        out["profile"] = prev["profile"]


def save(path: Path, key: str, out: dict) -> None:
    """File *out* under *key* in the JSON at *path*, leaving its other keys as they are."""
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    data[key] = out
    path.write_text(json.dumps(dict(sorted(data.items())), indent=1) + "\n", encoding="utf-8")


__all__ = ["best_of", "fit", "keep_previous", "load", "machine", "outputs_match", "profile_run",
           "rtl_bound", "save", "time_pysim", "time_rtl"]
