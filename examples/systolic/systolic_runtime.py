"""systolic_runtime.py -- what a product costs: the untimed bit-exact model, Waveflow's pysim, the RTL.

    python -m examples.systolic.systolic_runtime                          # untimed + pysim
    python -m examples.systolic.systolic_runtime --rtl --root C:/wfsc/rt  # ... + the RTL under XSI
    python -m examples.systolic.systolic_runtime --sizes --rtl --root C:/wfsc/rs  # across sizes
    python -m examples.systolic.systolic_runtime --conversion             # word conversion alone

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
checked against the model.  ``--conversion`` instead times the conversion between a matrix and its
message words (``lanes.to_words`` / ``from_words``) across square sizes, the direct path (machine-width
lanes) against the general ``ComplexField`` path, checking that both give the same words.  Times are the best of ``--reps`` runs: other load only ever adds time.
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


def time_pysim(cfg: dict, n: int, reps: int) -> tuple[float, int, bool, float]:
    """One whole pysim run -- build the harness, write the scenario, run -- its SimPy events,
    whether every output word equals the model's (checked after the clock stops), and its length
    in cycles."""
    from waveflow.build.system_dag import gather_recordings, golden_mismatches

    best, events, ok, cyc = None, 0, True, 0.0
    for _ in range(reps):
        with tempfile.TemporaryDirectory() as root:
            t = time.perf_counter()
            sysm = harness(EH.engine(**cfg), workload=f"steady_{n}", n_cycles=400 * n + 4000)
            sysm.write_scenario(Path(root))
            sysm.run()
            dt = time.perf_counter() - t
            events = next(sysm.sim.env._eid)
            cyc = sysm.sim.env.now / sysm.clock().period
            traces = Path(root) / "traces"
            gather_recordings(sysm, Path(root), traces)
            ok = ok and not golden_mismatches(traces, sysm.golden_outputs() or {})
        best = dt if best is None else min(best, dt)
    return best, events, ok, cyc


def rtl_bound(pysim_cycles: float) -> int:
    """The RTL's fixed run: the harness has no host to end it, so it simulates every cycle of its
    bound -- set a little past the pysim's length (within 12% of the RTL's), not far past it."""
    return int(1.2 * pysim_cycles) + 2000


def time_rtl(root: Path, cfg: dict, n: int, n_cycles: int) -> dict:
    """The harness through XSI in one build root: each step's time (csynth is fresh only on the
    first count), the XSI runner's phases, and whether compare passed."""
    from waveflow import events

    dag = EH.build_dag(cfg, workload=f"steady_{n}", n_cycles=n_cycles)
    with events.collect() as spans:
        results = dag.run(EH.build_config(root), through="compare")
    phases: dict[str, float] = {}
    for sp in spans:
        if sp.get("kind") == "phase":
            phases[sp["name"]] = phases.get(sp["name"], 0.0) + float(sp.get("elapsed", 0.0))
    bad = [k for k, r in results.items() if not r.success]
    return {"steps_s": {k: r.elapsed_seconds for k, r in results.items()}, "phases_s": phases,
            "exact": not bad, "failed": bad or None, "bound_cycles": n_cycles}


SQUARE_SIZES = (32, 64, 128, 256)


def time_sizes(sizes, reps: int, root: Path | None) -> dict:
    """One ``n x 32 x n`` product through the memory-backed engine (128-bit words, the flat pysim),
    per ``n``: the untimed model, the pysim (build the harness, run, every C word checked), and with
    *root* the RTL under XSI.  The RTL runs a fixed bound of cycles (the harness has no host to end
    it), set a little past the pysim's length; its rate is reported per simulated cycle, so its time
    for the product alone is ``done cycle / rate``."""
    from waveflow.utils.burst_io import read_burst_bundle

    from examples.systolic import engine_mem_harness as EMH

    cfg = {"word_bits": 128}
    rows = {}
    for n in sizes:
        kind = f"square_{n}"
        m = EMH.engine_mem(**cfg)
        jobs, _ = m._layout(kind)
        (_d, (ar, ai), (br, bi), _aw, _bw), = jobs
        best = None
        for _ in range(reps):
            t = time.perf_counter()
            mm.matmul(ar, ai, m.a, br, bi, m.b, m.c, form=int(m.form))
            best = min(best or 1e9, time.perf_counter() - t)
        row = {"M": n, "K": int(_d.K), "N": n, "untimed_s": best}

        best, cyc, events, exact = None, 0.0, 0, True
        for _ in range(reps):
            with tempfile.TemporaryDirectory() as tmp:
                t = time.perf_counter()
                s = harness(EMH.engine_mem(pysim="flat", **cfg), workload=kind, n_cycles=10**8)
                s.write_scenario(Path(tmp))
                s.run()
                best = min(best or 1e9, time.perf_counter() - t)
                cyc, events = s.sim.env.now / s.clock().period, next(s.sim.env._eid)
                words = np.asarray(read_burst_bundle(Path(tmp) / "vectors" / "mem")[0], np.uint64)
                exact = exact and EMH.check_c(words, kind, **cfg) == [0]
        row["pysim_flat"] = {"s": best, "cycles": cyc, "events": events, "exact": exact}

        if root is not None:
            bound = rtl_bound(cyc)
            r = time_rtl_mem(root, cfg, kind, bound)
            work = root / EMH.WORK_DIR / EMH.TOP / "xsi_run" / "vectors"
            words = np.asarray(read_burst_bundle(work / "mem")[0], np.uint64)
            done = np.fromfile(work / "s_done" / "cycles.bin", dtype="<u8")
            sim_s = r["phases_s"].get("simulate", r["steps_s"].get("system_xsi", 0.0))
            row["rtl"] = {**r, "bound_cycles": bound, "simulate_s": sim_s,
                          "done_cycle": int(done[-1]) if done.size else None,
                          "cycles_per_s": bound / sim_s if sim_s else None,
                          "exact": r["exact"] and EMH.check_c(words, kind, **cfg) == [0]}
        rows[str(n)] = row
        rt = row.get("rtl", {})
        print(f"{n:4d}x{row['K']}x{n:<4d} untimed {1e3 * row['untimed_s']:8.2f} ms  "
              f"pysim {1e3 * row['pysim_flat']['s']:8.1f} ms ({cyc:.0f} cyc, {events} ev)  "
              f"rtl {rt.get('simulate_s', float('nan')):7.2f} s ({rt.get('done_cycle')} cyc)  "
              f"exact {row['pysim_flat']['exact']} {rt.get('exact')}", flush=True)
    return {"workload": "n x 32 x n, SystolicEngineMem centre, 128-bit words", "sizes": rows}


def time_rtl_mem(root: Path, cfg: dict, kind: str, n_cycles: int) -> dict:
    """The memory-backed engine's harness through XSI (csynth fresh only on the first size)."""
    from waveflow import events

    from examples.systolic import engine_mem_harness as EMH

    dag = EMH.build_dag(cfg, workload=kind, n_cycles=n_cycles)
    with events.collect() as spans:
        results = dag.run(EMH.build_config(root), through="compare")
    phases: dict[str, float] = {}
    for sp in spans:
        if sp.get("kind") == "phase":
            phases[sp["name"]] = phases.get(sp["name"], 0.0) + float(sp.get("elapsed", 0.0))
    bad = [k for k, r in results.items() if not r.success]
    return {"steps_s": {k: r.elapsed_seconds for k, r in results.items()}, "phases_s": phases,
            "exact": not bad, "failed": bad or None}


CONV_SIZES = (8, 16, 32, 64, 128, 256, 512)


def _best_per_call(fn, reps: int, budget_s: float = 0.2) -> float:
    """Seconds a call, the best of *reps* batches of enough calls to fill about *budget_s*."""
    t = time.perf_counter()
    fn()
    k = max(1, int(budget_s / max(time.perf_counter() - t, 1e-7)))
    best = None
    for _ in range(reps):
        t = time.perf_counter()
        for _ in range(k):
            fn()
        dt = (time.perf_counter() - t) / k
        best = dt if best is None else min(best, dt)
    return best


def time_conversion(cfg: dict, sizes, reps: int) -> dict:
    """An ``n x n`` matrix of the engine's operand format to words and back, both paths, per call."""
    from waveflow.linalg import lanes as LN

    e = EH.engine(**cfg)
    fmt, lb, wb = e.a, int(e.lane_bits), int(e.word_bits)
    direct = LN._direct_dtype
    general = lambda *a: None  # noqa: E731 -- the general path, as for a lane width with no dtype
    rows = {}
    try:
        for n in sizes:
            rng = np.random.default_rng(n)
            lo, hi = -(1 << (fmt.W - 1)), 1 << (fmt.W - 1)
            re, im = rng.integers(lo, hi, (n, n)), rng.integers(lo, hi, (n, n))
            row, words = {}, {}
            for name, d in (("direct", direct), ("general", general)):
                LN._direct_dtype = d
                w = words[name] = LN.to_words(re, im, fmt, lb, wb)
                row[name] = {
                    "to_words_us": 1e6 * _best_per_call(lambda: LN.to_words(re, im, fmt, lb, wb), reps),
                    "from_words_us": 1e6 * _best_per_call(
                        lambda: LN.from_words(w, n * n, fmt, lb, wb), reps),
                }
            row["same_words"] = bool(np.array_equal(words["direct"], words["general"]))
            rows[str(n)] = row
            d, g = row["direct"], row["general"]
            print(f"{n:4d}x{n:<4d} to_words {g['to_words_us']:9.1f} -> {d['to_words_us']:8.1f} us"
                  f"   from_words {g['from_words_us']:9.1f} -> {d['from_words_us']:8.1f} us", flush=True)
    finally:
        LN._direct_dtype = direct
    return {"format": f"W={fmt.W} in {lb}-bit lanes", "sizes": rows}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--counts", type=int, nargs="+", default=list(COUNTS))
    ap.add_argument("--word-bits", type=int, default=128)
    ap.add_argument("--reps", type=int, default=3, help="best of this many runs per point")
    ap.add_argument("--rtl", action="store_true", help="also run the RTL under XSI (needs Vivado)")
    ap.add_argument("--root", type=Path, default=None, help="the RTL build root (keep it short)")
    ap.add_argument("--sizes", action="store_true",
                    help="n x 32 x n products through the memory-backed engine, n = 32 .. 256")
    ap.add_argument("--conversion", action="store_true",
                    help="only time the matrix <-> word conversion across sizes")
    a = ap.parse_args()
    counts = sorted(a.counts)
    cfg = {"word_bits": a.word_bits}
    machine = {"cpu": platform.processor(), "python": platform.python_version(),
               "system": platform.platform()}
    if a.sizes:
        root = None
        if a.rtl:
            if a.root is None:
                raise SystemExit("--rtl needs --root (a short path: csynth fails past 260 characters)")
            root = a.root.resolve()
            root.mkdir(parents=True, exist_ok=True)
        data = json.loads(RUNTIME.read_text(encoding="utf-8")) if RUNTIME.exists() else {}
        out = {**time_sizes(SQUARE_SIZES, a.reps, root), "date": date.today().isoformat(),
               "preliminary": True, "machine": machine, "reps": a.reps}
        if root is None and "sizes_w128" in data:        # keep RTL rows this run did not repeat
            for n, row in data["sizes_w128"].get("sizes", {}).items():
                if "rtl" in row and n in out["sizes"]:
                    out["sizes"][n]["rtl"] = row["rtl"]
        data["sizes_w128"] = out
        RUNTIME.write_text(json.dumps(dict(sorted(data.items())), indent=1) + "\n", encoding="utf-8")
        return
    if a.conversion:
        data = json.loads(RUNTIME.read_text(encoding="utf-8")) if RUNTIME.exists() else {}
        data[f"conversion_w{a.word_bits}"] = {
            **time_conversion(cfg, CONV_SIZES, a.reps), "word_bits": a.word_bits,
            "date": date.today().isoformat(), "preliminary": True, "machine": machine,
            "reps": a.reps}
        RUNTIME.write_text(json.dumps(dict(sorted(data.items())), indent=1) + "\n", encoding="utf-8")
        return
    out = {"workload": "8x8x32 back to back", "word_bits": a.word_bits,
           "date": date.today().isoformat(), "preliminary": True, "machine": machine,
           "counts": counts, "reps": a.reps}

    secs = [time_untimed(cfg, n, a.reps) for n in counts]
    out["untimed"] = _fit(counts, secs)
    print(f"untimed  {out['untimed']['per_product_ms']:8.3f} ms/product", flush=True)

    pysim_cycles = {}
    for mode, key in (("tasks", "pysim"), ("flat", "pysim_flat")):
        runs = [time_pysim({**cfg, "pysim": mode}, n, a.reps) for n in counts]
        pysim_cycles = pysim_cycles or {n: r[3] for n, r in zip(counts, runs)}
        out[key] = {**_fit(counts, [r[0] for r in runs]), "exact": all(r[2] for r in runs),
                    "events": {str(n): r[1] for n, r in zip(counts, runs)},
                    "cycles": {str(n): r[3] for n, r in zip(counts, runs)},
                    "events_per_product": float(np.polyfit(counts, [r[1] for r in runs], 1)[0])}
        print(f"{key:10} {out[key]['per_product_ms']:8.3f} ms/product  "
              f"{out[key]['events_per_product']:.0f} events/product", flush=True)

    if a.rtl:
        if a.root is None:
            raise SystemExit("--rtl needs --root (a short path: csynth fails past 260 characters)")
        root = a.root.resolve()
        root.mkdir(parents=True, exist_ok=True)
        rr = [time_rtl(root, cfg, n, rtl_bound(pysim_cycles[n])) for n in counts]
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
