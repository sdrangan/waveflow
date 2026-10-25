"""mf_runtime.py -- what a matched-filter frame on the vector unit costs: the untimed models, Waveflow's
pysim, the RTL.

    python -m examples.vecunit_mf.mf_runtime                                # untimed + pysim
    python -m examples.vecunit_mf.mf_runtime --rtl --root /tmp/vu_mf_rt     # ... + the RTL under XSI
    python -m examples.vecunit_mf.mf_runtime --profile                      # where pysim's time goes

The ways to run the same bit-exact matched filter on the same operands
(``docs/evaluation/sim_speed/vecunit_mf.md`` is written from what this records; ``plans/vecunit_new.md``,
Stage 8):

- **golden** -- the application's untimed model, ``mf_golden.mf_golden`` on the whole frame.
- **interpreter** -- the unit's untimed model, ``mf_program.run_mf`` on the frame's program.
- **pysim** -- ``MfVecUnit`` in its harness: building the harness (the stimulus), the run, and both
  together; the SimPy event count, which does not depend on the machine, beside the time.
- **rtl** -- the synthesized unit under XSI through the system flow, with csynth and the testbench's
  build reported apart from the simulation.

The sizes: one frame of ``nsamp`` samples at 8 antennas, ``nsamp`` from 256 to 16,384, on the unit as
built (``VLMAX 64, nrx_max 8, L 4``) -- 4 to 256 chunks of 64 samples (workload
``"frame_<nsamp>"``).  Every run is checked: the interpreter's ``Y`` against the golden's, pysim's
outputs against the interpreter's, the RTL's against pysim's.  Times are the best of ``--reps`` runs
(the RTL one run a size); results go to ``runtime.json`` with the machine.  Measurement only: nothing
here is optimized.
"""
from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path

import numpy as np

from examples.vecunit_mf.harness import unit
from examples.vecunit_mf.mf_golden import mf_golden
from examples.vecunit_mf.mf_program import chunks, run_mf
from waveflow.vecunit import runtime as RT

HERE = Path(__file__).resolve().parent
RUNTIME = HERE / "runtime.json"
KEY = "frame_vlmax64_nrx8"
SIZES = (256, 1024, 4096, 16384)


def time_untimed(nsamp: int, reps: int) -> dict:
    """The golden and the interpreter on the frame the harness plays.  The interpreter runs its
    passes instruction-major (``"serial"``, what pysim's execute task calls) and, beside it,
    element-major (``"pass"``, as the hardware; its default).  ``exact``: ``Y`` from the interpreter,
    both ways, equals the golden's."""
    u = unit()
    (_n, nrx, seed), = u.frames(f"frame_{nsamp}")
    xr, xi, rr, ri = u.frame(nsamp, nrx, seed)
    golden = lambda: mf_golden(xr, xi, u.fx, rr, ri, u.fr, u.fy)
    gr, gi = golden()
    exact = True
    for mode in ("serial", "pass"):
        yr, yi, _r = run_mf(u.cfg, xr, xi, rr, ri, mode=mode)
        exact = exact and bool(np.array_equal(yr, gr) and np.array_equal(yi, gi))
    return {"golden_s": RT.best_of(golden, reps),
            "interpreter_s": RT.best_of(lambda: run_mf(u.cfg, xr, xi, rr, ri, mode="serial"), reps),
            "interpreter_pass_s": RT.best_of(lambda: run_mf(u.cfg, xr, xi, rr, ri, mode="pass"),
                                             reps),
            "exact": exact}


def time_sizes(sizes, reps: int, root: Path | None) -> dict:
    """Per size: the untimed ways, the pysim, and with *root* the RTL (one build root for every size,
    so csynth runs once)."""
    rows = {}
    for nsamp in sizes:
        kind = f"frame_{nsamp}"
        u = unit()
        nrx = u.frames(kind)[0][1]
        row = {"nsamp": nsamp, "nrx": nrx, "chunks": len(chunks(u.cfg, nsamp)),
               "input_words": nsamp * (nrx + 1) // 2, **time_untimed(nsamp, reps)}
        row["pysim"] = RT.time_pysim(unit(), kind, reps)
        if root is not None:
            row["rtl"] = RT.time_rtl(unit(), kind, root, RT.rtl_bound(row["pysim"]["cycles"]))
        rows[str(nsamp)] = row
        p, r = row["pysim"], row.get("rtl", {})
        print(f"{nsamp:6d}x{nrx} ({row['chunks']:3d} chunks)  golden {1e3 * row['golden_s']:8.2f} ms  "
              f"interp {1e3 * row['interpreter_s']:8.1f} ms  pysim {1e3 * p['s']:8.1f} ms (build "
              f"{1e3 * p['build_s']:.1f}, run {1e3 * p['run_s']:.1f}; {p['cycles']:.0f} cyc, "
              f"{p['events']} ev)  rtl {r.get('rtl_s') or float('nan'):7.2f} s "
              f"({r.get('done_cycle')} cyc)  exact {row['exact']} {p['exact']} {r.get('exact')}",
              flush=True)
    return rows


def fits(rows: dict) -> dict:
    """Each way's ``setup + chunks * per chunk``, over the sizes."""
    ch = [r["chunks"] for r in rows.values()]
    out = {w: RT.fit(ch, [r[k] for r in rows.values()])
           for w, k in (("golden", "golden_s"), ("interpreter", "interpreter_s"),
                        ("interpreter_pass", "interpreter_pass_s"))}
    for w, k in (("pysim", "s"), ("pysim_build", "build_s"), ("pysim_run", "run_s")):
        out[w] = RT.fit(ch, [r["pysim"][k] for r in rows.values()])
    out["pysim_events"] = {"per": float(np.polyfit(ch, [r["pysim"]["events"] for r in rows.values()],
                                                   1)[0])}
    if all("rtl" in r and r["rtl"].get("rtl_s") for r in rows.values()):
        out["rtl"] = RT.fit(ch, [r["rtl"]["rtl_s"] for r in rows.values()])
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--sizes", type=int, nargs="+", default=list(SIZES), help="samples a frame")
    ap.add_argument("--reps", type=int, default=11, help="best of this many runs per point")
    ap.add_argument("--rtl", action="store_true", help="also run the RTL under XSI (needs Vivado)")
    ap.add_argument("--root", type=Path, default=None, help="the RTL build root")
    ap.add_argument("--profile", action="store_true",
                    help="only profile one pysim run at the largest size")
    a = ap.parse_args()
    if a.profile:
        n = max(a.sizes)
        prof = {"size": n, **RT.profile_run(unit(), f"frame_{n}")}
        print({k: round(v, 3) if isinstance(v, float) else v for k, v in prof.items()})
        rec = RT.load(RUNTIME, KEY)
        if not rec:
            raise SystemExit(f"run the sizes first: {RUNTIME} has no {KEY!r}")
        RT.save(RUNTIME, KEY, {**rec, "profile": prof})
        return
    root = None
    if a.rtl:
        if a.root is None:
            raise SystemExit("--rtl needs --root")
        root = a.root.resolve()
        root.mkdir(parents=True, exist_ok=True)
    rows = time_sizes(sorted(a.sizes), a.reps, root)
    out = {"workload": "one frame of nsamp samples at 8 antennas; MfVecUnit (VLMAX 64, nrx_max 8, "
                       "L 4), 64-bit words",
           "date": datetime.now().astimezone().date().isoformat(), "machine": RT.machine(),
           "reps": a.reps, "sizes": rows}
    RT.keep_previous(RUNTIME, KEY, out)
    out["fit"] = fits(rows) if len(rows) > 1 else None
    RT.save(RUNTIME, KEY, out)


if __name__ == "__main__":
    main()
