"""fir_calibrate.py — occupancy-based, near-fit-free cycle model from the cosim sweep (Stage B).

Replaces the retired matrix-LT calibration.  The free-running kernel's timing is **composed from
physical, deterministic components**, not fitted end-to-end (the matrix-LT philosophy,
``project-matrix-lt-fir-build``; and the empirical finding that span-based end-to-end fits are
contaminated by stalls + interleaved beats — measure *occupancy*, not span):

  * **bus occupancy = transfer beats == nwords** — asserted exact across the sweep (the load moves
    ``read_words`` beats, the store ``write_words``).  Zero fit.
  * **compute II=1** — ``n_rows*n_cols`` input samples.  Zero fit (``compute_beat_cyc = 1``).
  * the load + store **serialize on the one ``gmem`` bundle in practice** (VCD: <10% burst
    overlap), so per job the bus moves ``read_words + write_words`` beats — a structural
    composition, not a fit.  (NB the bundle itself is full-duplex — ``sandbox/duplex_toy`` shows
    read+write cosim ≈ max for both one process and two DATAFLOW processes — so this is the FIR's
    dataflow-dynamics serialization, ~2× throughput headroom, not a bus limit.)

The **single calibrated residual** is ``beta`` — the m_axi sustained cyc/beat (the Vitis
random-stall efficiency) — fit from ``period`` vs ``occupancy`` (slope = ``beta``, intercept =
``bus_job_cyc``, the per-job address/command setup).  ``pipe_fill_cyc`` (the command stream-in +
DATAFLOW fill/drain lead-in) is then set from the single-job-latency residual.  The end-to-end
period/latency are **emergent** (the sim composes the components over the shared bus) and are
*validated*, not fitted target-by-target — incl. a held-out size point.

Usage (project venv; reads results/cosim_sweep.json from fir_sweep.py --sweep)::

    PYTHONPATH=../../.. ../../../pysilicon-venv/Scripts/python.exe fir_calibrate.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(REPO))

from examples.rowwise_fir.fir_golden import T   # noqa: E402

RESULTS = HERE / "results"
SWEEP_JSON = RESULTS / "cosim_sweep.json"
CALIB_JSON = RESULTS / "fir_calibration.json"

COMPUTE_BEAT_CYC = 1.0          # II=1: one input sample per cycle (physical, not fitted)


def _occ(pt: dict) -> int:
    j0 = pt["per_job"][0]
    return j0["load_words"] + j0["store_words"]


def assert_occupancy_deterministic(points: list[dict]) -> None:
    """The component check: every transfer's beats == nwords (the deterministic occupancy)."""
    for p in points:
        j0 = p["per_job"][0]
        if j0["load_words"] != p["read_words"] or j0["store_words"] != p["write_words"]:
            raise AssertionError(
                f"occupancy not deterministic at {p['n_row']}x{p['n_col']}: "
                f"load {j0['load_words']} vs read {p['read_words']}, "
                f"store {j0['store_words']} vs write {p['write_words']}")


def fit(points: list[dict], holdout: tuple[int, int]) -> dict:
    """Fit ``beta`` + ``bus_job_cyc`` from period-vs-occupancy, then ``pipe_fill_cyc`` from the
    single-job-latency residual.  Excludes the holdout from the fit."""
    train = [p for p in points if (p["n_row"], p["n_col"]) != holdout]
    occ = np.array([_occ(p) for p in train], dtype=float)
    per = np.array([p["period_cyc"] for p in train], dtype=float)
    beta, bus_job = np.linalg.lstsq(np.vstack([occ, np.ones_like(occ)]).T, per, rcond=None)[0]

    # pipe_fill: mean( L1 - [read*beta + bus_job + n_rows*n_cols*compute_beat + write*beta] )
    resid = []
    for p in train:
        rw = p["read_words"] * beta + bus_job + p["write_words"] * beta
        comp = p["n_row"] * p["n_col"] * COMPUTE_BEAT_CYC
        resid.append(p["single_job_latency_cyc"] - (rw + comp))
    pipe_fill = float(np.mean(resid))
    return {"bus_beat_cyc": round(float(beta), 4), "bus_job_cyc": round(float(bus_job), 2),
            "compute_beat_cyc": COMPUTE_BEAT_CYC, "pipe_fill_cyc": round(pipe_fill, 2)}


# --- emergent validation (run the actual sim with the fitted params) -----------

def _sim_period(nr: int, nc: int, n_jobs: int = 4) -> float:
    """Steady per-job period from the sim: a mid (fully-contended) inter-job store-end spacing
    (NOT the final spacing, which is the pipeline drain)."""
    from examples.rowwise_fir.fir_sim import FIRSim, make_specs
    sim = FIRSim(make_specs([(nr, nc)] * n_jobs, seed=0))
    sim.run()
    se = [next(e["cyc"] for e in sim.accel.events if e["event"] == "store_end" and e["tx_id"] == t)
          for t in range(n_jobs)]
    sp = [se[i + 1] - se[i] for i in range(n_jobs - 1)]
    return sp[len(sp) // 2 - 1]


def _sim_latency(nr: int, nc: int) -> float:
    """Single-job latency from the sim: store_end − cmd_arrive (no inter-job store contention)."""
    from examples.rowwise_fir.fir_sim import FIRSim, make_specs
    sim = FIRSim(make_specs([(nr, nc)], seed=0))
    sim.run()

    def c(name: str) -> float:
        return next(e["cyc"] for e in sim.accel.events if e["event"] == name and e["tx_id"] == 0)

    return c("store_end") - c("cmd_arrive")


def validate(points: list[dict], holdout: tuple[int, int]) -> dict:
    """Gate B — the **emergent** sim (with the fitted params just written) vs the cosim sweep,
    per size, including the held-out point.  No extra Vitis."""
    rows = []
    for p in points:
        nr, nc = p["n_row"], p["n_col"]
        sp, sl = _sim_period(nr, nc), _sim_latency(nr, nc)
        rec = {"point": [nr, nc], "held_out": (nr, nc) == holdout,
               "occupancy": _occ(p),
               "period": {"sim": round(sp, 1), "cosim": p["period_cyc"],
                          "rel_err": round(abs(sp - p["period_cyc"]) / p["period_cyc"], 4)},
               "latency": {"sim": round(sl, 1), "cosim": p["single_job_latency_cyc"],
                           "rel_err": round(abs(sl - p["single_job_latency_cyc"])
                                            / p["single_job_latency_cyc"], 4)}}
        rows.append(rec)
    amortized = [r for r in rows if r["occupancy"] >= 128]   # the throughput regime
    ho = next(r for r in rows if r["held_out"])
    return {"per_point": rows, "holdout": ho,
            "worst_period_rel_err": round(max(r["period"]["rel_err"] for r in rows), 4),
            "worst_latency_rel_err": round(max(r["latency"]["rel_err"] for r in rows), 4),
            "worst_period_rel_err_amortized": round(max(r["period"]["rel_err"] for r in amortized), 4),
            "worst_latency_rel_err_amortized": round(max(r["latency"]["rel_err"] for r in amortized), 4)}


def main() -> None:
    data = json.loads(SWEEP_JSON.read_text(encoding="utf-8"))
    points = data["points"]
    holdout = tuple(data["holdout"])
    assert_occupancy_deterministic(points)
    print("occupancy deterministic (beats == nwords): OK for all "
          f"{len(points)} points")

    params = fit(points, holdout)
    print(f"fitted: {params}")

    calib = {
        "model": "occupancy-based, near-fit-free: bus occupancy = beats == nwords (exact); "
                 "compute II=1 (exact); FIR serializes load+store on the shared gmem bundle "
                 "(VCD <10% overlap; bundle is full-duplex per duplex_toy -> ~2x headroom), so "
                 "read+write occupancy ADD; single residual beta = m_axi sustained cyc/beat "
                 "(period vs occupancy); end-to-end period/latency EMERGENT, validated not fitted.",
        "T": T, "clk_ns": data["points"][0]["clk_ns"], "njobs": data["njobs"],
        "holdout": list(holdout), "model_params": params,
        "occupancy_deterministic": True,
    }
    # write params first so the sim (FIRTiming.from_calibration) picks them up for validation
    CALIB_JSON.write_text(json.dumps(calib, indent=2) + "\n", encoding="utf-8")
    calib["validation"] = validate(points, holdout)
    CALIB_JSON.write_text(json.dumps(calib, indent=2) + "\n", encoding="utf-8")

    v = calib["validation"]
    print("\n=== Gate B (emergent sim with fitted params vs cosim) ===")
    print(f"worst period   rel_err: all {v['worst_period_rel_err']*100:.1f}%  "
          f"amortized {v['worst_period_rel_err_amortized']*100:.1f}%")
    print(f"worst latency  rel_err: all {v['worst_latency_rel_err']*100:.1f}%  "
          f"amortized {v['worst_latency_rel_err_amortized']*100:.1f}%")
    print(f"holdout {v['holdout']['point']}: period {v['holdout']['period']}, "
          f"latency {v['holdout']['latency']}")
    print(f"\nwrote {CALIB_JSON}")


if __name__ == "__main__":
    main()
