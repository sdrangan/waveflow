"""cg_runtime.py -- what a CG solve on the vector unit costs: the untimed models, Waveflow's pysim, the RTL.

    python -m examples.vecunit_cg.cg_runtime                                # untimed + pysim
    python -m examples.vecunit_cg.cg_runtime --rtl --root /tmp/vu_cg_rt     # ... + the RTL under XSI
    python -m examples.vecunit_cg.cg_runtime --profile                      # where pysim's time goes

The ways to run the same bit-exact CG on the same operands (``docs/evaluation/sim_speed/vecunit_cg.md``
is written from what this records; ``plans/vecunit_new.md``, Stage 8):

- **golden** -- the application's untimed model, ``waveflow.linalg.cg.cg_solve`` on the whole ``B``,
  every column at once.
- **interpreter** -- the unit's untimed model, ``cg_program.run_cg`` a job at a time (closed loop: the
  harness's ``mm_step`` on each ``P`` the unit emits).
- **pysim** -- ``CgVecUnit`` in its harness: building the harness (the stimulus, whose ``S`` matrices
  come from a closed-loop interpreter run), the run, and both together; the SimPy event count, which
  does not depend on the machine, beside the time.
- **rtl** -- the synthesized unit under XSI through the system flow, with csynth and the testbench's
  build reported apart from the simulation.

The sizes: one ``8 x n`` right-hand side of three iterations, ``n`` from 32 to 2,048, on the unit's
centre build (``Kmax 8, Nmax 32, L 4``) -- ``n / 32`` jobs, a column block each (workload
``"rhs_<n>"``).  Every run is checked: the interpreter's ``X`` against ``cg_solve``'s, pysim's outputs
against the interpreter's, the RTL's against pysim's.  Times are the best of ``--reps`` runs (the RTL
one run a size); results go to ``runtime.json`` with the machine.  Measurement only: nothing here is
optimized.
"""
from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path

import numpy as np

from examples.vecunit_cg.cg_program import PORT, run_cg
from examples.vecunit_cg.cg_unit import RHS_NIT
from examples.vecunit_cg.harness import unit
from waveflow.linalg import cg
from waveflow.vecunit import runtime as RT

HERE = Path(__file__).resolve().parent
RUNTIME = HERE / "runtime.json"
KEY = "rhs_centre"
SIZES = (32, 128, 512, 2048)


def record_key(word_bits: int) -> str:
    """The record's key in ``runtime.json``: :data:`KEY`, with the word width when it is not 64."""
    return KEY if word_bits == 64 else f"{KEY}_w{word_bits}"


def time_untimed(n: int, reps: int, word_bits: int = 64) -> dict:
    """The golden on the whole ``B`` and the interpreter a column block at a time, on the operands
    the harness plays.  The interpreter runs its passes instruction-major (``"serial"``, what pysim's
    execute task calls) and, beside it, element-major (``"pass"``, as the hardware; its default).
    ``exact``: every block's ``X`` from the interpreter, both ways, equals the golden's."""
    u = unit(word_bits=word_bits)
    f, cfg, N = u.cg_formats, u.cfg, int(u.Nmax)
    ar, ai, br, bi = u.rhs(n)
    blocks = range(0, n, N)
    solve = lambda: cg.cg_solve(ar, ai, br, bi, RHS_NIT, f)

    def interp(mode):
        def solve():  # one solve: its column blocks share a program, checked once
            checks: dict = {}
            return [run_cg(cfg, f, ar, ai, br[:, c:c + N], bi[:, c:c + N], RHS_NIT, mode=mode,
                           checks=checks) for c in blocks]
        return solve

    gold, exact = solve(), True
    for mode in ("serial", "pass"):
        runs = interp(mode)()
        xr = np.concatenate([r.outputs[PORT][-1][1] for r in runs], axis=1)
        xi = np.concatenate([r.outputs[PORT][-1][2] for r in runs], axis=1)
        exact = exact and bool(np.array_equal(xr, gold.xr) and np.array_equal(xi, gold.xi))
    return {"golden_s": RT.best_of(solve, reps),
            "interpreter_s": RT.best_of(interp("serial"), reps),
            "interpreter_pass_s": RT.best_of(interp("pass"), reps), "exact": exact}


def time_sizes(sizes, reps: int, root: Path | None, word_bits: int = 64) -> dict:
    """Per size: the untimed ways, the pysim, and with *root* the RTL (one build root for every size,
    so csynth runs once)."""
    rows = {}
    for n in sizes:
        kind = f"rhs_{n}"
        row = {"k": 8, "n": n, "jobs": n // 32, **time_untimed(n, reps, word_bits)}
        row["pysim"] = RT.time_pysim(unit(word_bits=word_bits), kind, reps)
        if root is not None:
            row["rtl"] = RT.time_rtl(unit(word_bits=word_bits), kind, root,
                                    RT.rtl_bound(row["pysim"]["cycles"]))
        rows[str(n)] = row
        p, r = row["pysim"], row.get("rtl", {})
        print(f"8x{n:<5d} ({row['jobs']:2d} jobs)  golden {1e3 * row['golden_s']:8.2f} ms  "
              f"interp {1e3 * row['interpreter_s']:8.1f} ms  pysim {1e3 * p['s']:8.1f} ms (build "
              f"{1e3 * p['build_s']:.1f}, run {1e3 * p['run_s']:.1f}; {p['cycles']:.0f} cyc, "
              f"{p['events']} ev)  rtl {r.get('rtl_s') or float('nan'):7.2f} s "
              f"({r.get('done_cycle')} cyc)  exact {row['exact']} {p['exact']} {r.get('exact')}",
              flush=True)
    return rows


def fits(rows: dict) -> dict:
    """Each way's ``setup + jobs * per job``, over the sizes."""
    jobs = [r["jobs"] for r in rows.values()]
    out = {w: RT.fit(jobs, [r[k] for r in rows.values()])
           for w, k in (("golden", "golden_s"), ("interpreter", "interpreter_s"),
                        ("interpreter_pass", "interpreter_pass_s"))}
    for w, k in (("pysim", "s"), ("pysim_build", "build_s"), ("pysim_run", "run_s")):
        out[w] = RT.fit(jobs, [r["pysim"][k] for r in rows.values()])
    out["pysim_events"] = {"per": float(np.polyfit(jobs, [r["pysim"]["events"] for r in rows.values()],
                                                   1)[0])}
    if all("rtl" in r and r["rtl"].get("rtl_s") for r in rows.values()):
        out["rtl"] = RT.fit(jobs, [r["rtl"]["rtl_s"] for r in rows.values()])
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--sizes", type=int, nargs="+", default=list(SIZES), help="widths n of B")
    ap.add_argument("--reps", type=int, default=11, help="best of this many runs per point")
    ap.add_argument("--rtl", action="store_true", help="also run the RTL under XSI (needs Vivado)")
    ap.add_argument("--root", type=Path, default=None, help="the RTL build root")
    ap.add_argument("--word-bits", type=int, default=64, help="the data ports' width: 64 or 128")
    ap.add_argument("--profile", action="store_true",
                    help="only profile one pysim run at the largest size")
    a = ap.parse_args()
    if a.profile:
        n = max(a.sizes)
        prof = {"size": n, **RT.profile_run(unit(word_bits=a.word_bits), f"rhs_{n}")}
        print({k: round(v, 3) if isinstance(v, float) else v for k, v in prof.items()})
        key = record_key(a.word_bits)
        rec = RT.load(RUNTIME, key)
        if not rec:
            raise SystemExit(f"run the sizes first: {RUNTIME} has no {key!r}")
        RT.save(RUNTIME, key, {**rec, "profile": prof})
        return
    root = None
    if a.rtl:
        if a.root is None:
            raise SystemExit("--rtl needs --root")
        root = a.root.resolve()
        root.mkdir(parents=True, exist_ok=True)
    rows = time_sizes(sorted(a.sizes), a.reps, root, a.word_bits)
    out = {"workload": f"8 x n right-hand side, {RHS_NIT} iterations, as n/32 jobs of 8 x 32; "
                       f"CgVecUnit centre (Kmax 8, Nmax 32, L 4), {a.word_bits}-bit words",
           "date": datetime.now().astimezone().date().isoformat(), "machine": RT.machine(),
           "reps": a.reps, "sizes": rows}
    key = record_key(a.word_bits)
    RT.keep_previous(RUNTIME, key, out)
    out["fit"] = fits(rows) if len(rows) > 1 else None
    RT.save(RUNTIME, key, out)


if __name__ == "__main__":
    main()
