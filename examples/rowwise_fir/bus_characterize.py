"""bus_characterize.py — one-time PLATFORM bus characterization from a pure copy kernel.

Level 1 of the two-level calibration (see project-two-level-calibration): the bus transfer time is
a property of the *memory + AXI interconnect*, the same for any accelerator dropped on this
platform — so it is characterized ONCE, from a kernel with **no compute**, and reused.  Only the
kernel COMPUTE is calibrated per accelerator (fir_calibrate.py).

Method: cosim ``sandbox/loadstore_iso`` (free-running load‖store, lane loop, max_burst=256 like the
generated kernels) across transfer sizes N.  For each job we read the per-direction channel
**occupancy** — AR/AW-issue to last data beat (setup + beats), independent of inter-job overlap —
and fit (through-origin)

    occupancy(N) = setup·num_trans + per_word·nwords      (num_trans = ceil(N / 256))

per direction, recovering ``setup`` (per-burst address latency) and ``per_word`` (≈1 beat/cycle for
a healthy II=1 burst).  These are exactly the platform ``BusTiming`` params the FIR sim configures
on the memory slave (``fir_sim.py``: BUS_SETUP_CYC / BUS_PER_WORD_CYC).

Run (project venv, Vitis 2025.1 + xsim), from the repo root::

    PYTHONPATH=. pysilicon-venv/Scripts/python.exe examples/rowwise_fir/bus_characterize.py

Writes ``results/bus_char.json`` (read/write setup+per_word + points).  ``--fit-only`` re-fits from
the saved points file with no cosim.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from math import ceil
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(HERE / "sandbox" / "loadstore_iso"))

from waveflow.calib import CalibDataFrame, LinCalibModel  # noqa: E402

CHUNK = 256                      # max_burst_length in the kernel -> bursts of <=256 words
N_SWEEP = [64, 128, 256, 512]    # 64/128/256 are single-burst (num_trans=1); 512 -> num_trans=2
NJ = 4                           # jobs per cosim; per-job occupancy is taken over jobs[1:] (skip warmup)
RESULTS = HERE / "results"
POINTS_JSON = RESULTS / "bus_char_points.json"
BUS_CHAR_JSON = RESULTS / "bus_char.json"


def measure_point(n: int) -> dict:
    """Cosim one copy sweep at transfer size *n*; return the median per-direction occupancy (cyc)."""
    from run_loadstore_iso import run as ls_run  # sandbox runner (cosim + VCD burst parse)

    r = ls_run(n, NJ)
    jobs = r["jobs"]

    def med(key: str) -> float | None:
        vals = [j[key] for j in jobs[1:] if j.get(key) is not None]  # skip job 0 (pipeline warmup)
        return round(statistics.median(vals), 2) if vals else None

    return {"n": n, "num_trans": ceil(n / CHUNK), "nwords": n,
            "read_occ": med("load_occ"), "write_occ": med("store_occ"),
            "period": r["period"], "overlap_cyc": r.get("load_store_overlap_cyc")}


def sweep() -> list[dict]:
    RESULTS.mkdir(parents=True, exist_ok=True)
    points: list[dict] = []
    for n in N_SWEEP:
        print(f"[bus_char] cosim copy N={n} nj={NJ} ...", flush=True)
        try:
            p = measure_point(n)
        except Exception as e:  # keep going; a single point failing shouldn't lose the sweep
            print(f"[bus_char]   N={n} FAILED: {e}", flush=True)
            continue
        print(f"[bus_char]   N={n} num_trans={p['num_trans']} read_occ={p['read_occ']} "
              f"write_occ={p['write_occ']} cyc", flush=True)
        points.append(p)
        POINTS_JSON.write_text(json.dumps(points, indent=2), encoding="utf-8")  # incremental
    return points


def _fit_dir(db: CalibDataFrame, target: str) -> dict:
    m = LinCalibModel(basis=["num_trans", "nwords"], target=target,
                      fit_intercept=False, coeff_names=["setup", "per_word"]).fit(db)
    c = m.coeffs
    return {"setup": round(c["setup"], 4), "per_word": round(c["per_word"], 4),
            "r2": round(m.score(db), 5)}


def fit_and_write(points: list[dict]) -> dict:
    """Fit occupancy = setup·num_trans + per_word·nwords per direction; write the bus artifact.

    Fit on **single-burst** points only (num_trans==1): there the per-direction occupancy is a clean
    ``setup + per_word·nwords``.  Multi-burst points (num_trans>1) are EXCLUDED from the fit — in the
    copy kernel the store starves for load's data across bursts (a pipeline pacing artifact, NOT a
    bus inter-burst gap), which inflates their occupancy; they're kept in ``points`` as diagnostics.
    """
    usable = [p for p in points if p.get("read_occ") and p.get("write_occ")]
    fit_pts = [p for p in usable if p["num_trans"] == 1]
    if len(fit_pts) < 2:
        raise RuntimeError(f"need >=2 single-burst points to fit, have {len(fit_pts)}")
    db = CalibDataFrame(["num_trans", "nwords", "read_occ", "write_occ"])
    db.extend([{"num_trans": p["num_trans"], "nwords": p["nwords"],
                "read_occ": p["read_occ"], "write_occ": p["write_occ"]} for p in fit_pts])
    out = {"read": _fit_dir(db, "read_occ"), "write": _fit_dir(db, "write_occ"),
           "chunk": CHUNK, "nj": NJ, "fit_on": "single-burst (num_trans==1)",
           "note": "multi-burst points excluded (copy-kernel store-starve pacing artifact, not a bus gap)",
           "points": usable}
    BUS_CHAR_JSON.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fit-only", action="store_true",
                    help="re-fit from results/bus_char_points.json, no cosim")
    args = ap.parse_args()

    points = json.loads(POINTS_JSON.read_text(encoding="utf-8")) if args.fit_only else sweep()
    out = fit_and_write(points)
    for d in ("read", "write"):
        r = out[d]
        print(f"\n[bus_char] {d.upper()} occupancy = setup·num_trans + per_word·nwords:")
        print(f"  setup    = {r['setup']} cyc/burst   per_word = {r['per_word']} cyc/word"
              f"   R^2 = {r['r2']}")
    print(f"\n  wrote {BUS_CHAR_JSON}  ({len(out['points'])} points)")


if __name__ == "__main__":
    main()
