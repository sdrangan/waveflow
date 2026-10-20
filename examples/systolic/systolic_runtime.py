"""systolic_runtime.py -- what a product costs: the untimed bit-exact model, Waveflow's pysim, the RTL.

    python -m examples.systolic.systolic_runtime                          # untimed + pysim
    python -m examples.systolic.systolic_runtime --rtl --root C:/wfsc/rt  # ... + the RTL under XSI

The three ways to run the same bit-exact systolic engine on the same products -- ``8×8×32`` back to
back at the centre configuration (``docs/evaluation/sim_speed/systolic.md`` is written from what this
records):

- **untimed** -- the model alone, ``waveflow.linalg.matmul.matmul`` a product at a time.
- **pysim** -- the engine in its harness (``engine_harness``), both granularities: ``"tasks"`` (six
  tasks, every internal channel timed) and ``"flat"`` (one process, the tasks passive); each one's SimPy
  event count, which does not depend on the machine, is recorded beside its time.
- **rtl** -- the synthesized RTL under XSI through the system flow (``system_xsi``), with csynth and
  the testbench's build reported apart from the simulation.

Each is run at several product counts and fitted to ``setup + products * per_product``.  Every run is
checked against the model.  Times are the best of ``--reps`` runs: other load only ever adds time.
**Times taken on a machine in use are preliminary**; results go to ``runtime.json`` with the machine.
"""
from __future__ import annotations

import argparse
import json
import platform
import tempfile
import time
from datetime import date
from pathlib import Path

import numpy as np

from examples.systolic import engine_harness as EH
from waveflow.hw.harness import harness
from waveflow.linalg import matmul as mm

HERE = Path(__file__).resolve().parent
RUNTIME = HERE / "runtime.json"
COUNTS = (10, 50, 100)


def _fit(counts: list[int], secs: list[float]) -> dict:
    """``secs ~ setup + products * per_product``, least squares."""
    per, setup = np.polyfit(np.asarray(counts, float), np.asarray(secs, float), 1)
    return {"per_product_ms": 1e3 * per, "setup_ms": 1e3 * setup,
            "runs": {str(n): s for n, s in zip(counts, secs)}}


def time_untimed(cfg: dict, n: int, reps: int) -> float:
    """The model alone, on the operands the harness plays."""
    e = EH.engine(**cfg)
    ops = list(e._operands(f"steady_{n}"))
    best = None
    for _ in range(reps):
        t = time.perf_counter()
        for _c, o in ops:
            ar, ai = o["A", 0, 0]
            br, bi = o["B", 0, 0]
            mm.matmul(ar, ai, e.a, br, bi, e.b, e.c, form=int(e.form))
        dt = time.perf_counter() - t
        best = dt if best is None else min(best, dt)
    return best


def time_pysim(cfg: dict, n: int, reps: int) -> tuple[float, int, bool]:
    """One whole pysim run -- build the harness, write the scenario, run -- its SimPy events, and
    whether every output word equals the model's (checked after the clock stops)."""
    from waveflow.build.system_dag import gather_recordings, golden_mismatches

    best, events, ok = None, 0, True
    for _ in range(reps):
        with tempfile.TemporaryDirectory() as root:
            t = time.perf_counter()
            sysm = harness(EH.engine(**cfg), workload=f"steady_{n}", n_cycles=400 * n + 4000)
            sysm.write_scenario(Path(root))
            sysm.run()
            dt = time.perf_counter() - t
            events = next(sysm.sim.env._eid)
            traces = Path(root) / "traces"
            gather_recordings(sysm, Path(root), traces)
            ok = ok and not golden_mismatches(traces, sysm.golden_outputs() or {})
        best = dt if best is None else min(best, dt)
    return best, events, ok


def time_rtl(root: Path, cfg: dict, n: int) -> dict:
    """The harness through XSI in one build root: each step's time (csynth is fresh only on the
    first count), the XSI runner's phases, and whether compare passed."""
    from waveflow import events

    dag = EH.build_dag(cfg, workload=f"steady_{n}", n_cycles=400 * n + 4000)
    with events.collect() as spans:
        results = dag.run(EH.build_config(root), through="compare")
    phases: dict[str, float] = {}
    for sp in spans:
        if sp.get("kind") == "phase":
            phases[sp["name"]] = phases.get(sp["name"], 0.0) + float(sp.get("elapsed", 0.0))
    bad = [k for k, r in results.items() if not r.success]
    return {"steps_s": {k: r.elapsed_seconds for k, r in results.items()}, "phases_s": phases,
            "exact": not bad, "failed": bad or None}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--counts", type=int, nargs="+", default=list(COUNTS))
    ap.add_argument("--word-bits", type=int, default=128)
    ap.add_argument("--reps", type=int, default=3, help="best of this many runs per point")
    ap.add_argument("--rtl", action="store_true", help="also run the RTL under XSI (needs Vivado)")
    ap.add_argument("--root", type=Path, default=None, help="the RTL build root (keep it short)")
    a = ap.parse_args()
    counts = sorted(a.counts)
    cfg = {"word_bits": a.word_bits}
    out = {"workload": "8x8x32 back to back", "word_bits": a.word_bits,
           "date": date.today().isoformat(), "preliminary": True,
           "machine": {"cpu": platform.processor(), "python": platform.python_version(),
                       "system": platform.platform()},
           "counts": counts, "reps": a.reps}

    secs = [time_untimed(cfg, n, a.reps) for n in counts]
    out["untimed"] = _fit(counts, secs)
    print(f"untimed  {out['untimed']['per_product_ms']:8.3f} ms/product", flush=True)

    for mode, key in (("tasks", "pysim"), ("flat", "pysim_flat")):
        runs = [time_pysim({**cfg, "pysim": mode}, n, a.reps) for n in counts]
        out[key] = {**_fit(counts, [r[0] for r in runs]), "exact": all(r[2] for r in runs),
                    "events": {str(n): r[1] for n, r in zip(counts, runs)},
                    "events_per_product": float(np.polyfit(counts, [r[1] for r in runs], 1)[0])}
        print(f"{key:10} {out[key]['per_product_ms']:8.3f} ms/product  "
              f"{out[key]['events_per_product']:.0f} events/product", flush=True)

    if a.rtl:
        if a.root is None:
            raise SystemExit("--rtl needs --root (a short path: csynth fails past 260 characters)")
        root = a.root.resolve()
        root.mkdir(parents=True, exist_ok=True)
        rr = [time_rtl(root, cfg, n) for n in counts]
        sim = [r["phases_s"].get("simulate", r["steps_s"].get("system_xsi", 0.0)) for r in rr]
        out["rtl"] = {**_fit(counts, sim), "exact": all(r["exact"] for r in rr),
                      "csynth_s": rr[0]["steps_s"].get("csynth"), "runs_detail": rr}
        print(f"rtl      {out['rtl']['per_product_ms']:8.3f} ms/product (simulation)  "
              f"csynth {out['rtl']['csynth_s']} s  exact={out['rtl']['exact']}", flush=True)

    data = json.loads(RUNTIME.read_text(encoding="utf-8")) if RUNTIME.exists() else {}
    key = f"steady_w{a.word_bits}"
    if not a.rtl and "rtl" in data.get(key, {}):    # keep an RTL run this one did not repeat
        out["rtl"] = data[key]["rtl"]
    data[key] = out
    RUNTIME.write_text(json.dumps(dict(sorted(data.items())), indent=1) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
