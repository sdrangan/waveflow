"""fir_calibrate.py — fit the per-kernel COMPUTE models from the cosim sweep; validate (Gate B).

Level 2 of the two-level calibration (see project-two-level-calibration): the bus transfer time is
the platform's (bus_characterize.py), configured on the memory slave.  Here we fit ONLY the
accelerator's compute, from a contention-free per-job measurement on the bus:

  * **fill**        = t(first Y-write) − t(first X-read)                 -> fill_model  (L0)
  * **compute_body** = t(last Y-write) − t(first Y-write) = store_span   -> compute_model
                     (row_setup·(n_row-1) + beat·bulk; the II=1 production span the store hides under)

both read straight out of ``fir_sweep.py``'s per-job VCD burst spans (``fill_cyc`` / ``store_span_cyc``).
The models are :class:`~waveflow.calib.LinCalibModel`s (fir.py ``make_fill_model`` /
``make_compute_model``); ``fit(corpus).save_model()`` drops the per-model artifacts
(``fir_fill_model.json`` / ``fir_compute_model.json``) the sim loads.

**Gate B** — the payoff of the two-level split: with the platform bus model + these compute fits
(both fit in ISOLATION), the LOADED end-to-end period/latency must fall out of the sim with **zero
end-to-end fitting**.  ``validate()`` runs the sim (which just loaded the fitted models) per size and
compares to the cosim sweep's period + single-job latency, incl. a held-out size.

Usage (project venv; reads results/cosim_sweep.json from ``fir_sweep.py --sweep``)::

    PYTHONPATH=../../.. ../../../pysilicon-venv/Scripts/python.exe fir_calibrate.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(REPO))

from waveflow.calib import CalibDataFrame                                   # noqa: E402
from examples.rowwise_fir.fir import make_compute_model, make_fill_model   # noqa: E402
from examples.rowwise_fir.fir_golden import T                              # noqa: E402

RESULTS = HERE / "results"
SWEEP_JSON = RESULTS / "cosim_sweep.json"
REPORT_JSON = RESULTS / "fir_calibration_report.json"


# --- corpus: per-point job-0 fill + compute_body (contention-free), in TIME (clk_period folded) --

def corpus(points: list[dict]) -> CalibDataFrame:
    db = CalibDataFrame(["n_row", "n_col", "clk_period", "fill_time", "compute_time",
                         "fill_cyc", "compute_cyc"])
    for p in points:
        j0 = p["per_job"][0]
        if j0.get("fill_cyc") is None or j0.get("compute_cyc") is None:
            continue
        cp = float(p["clk_ns"]) * 1e-9
        db.add_datapoint({"n_row": p["n_row"], "n_col": p["n_col"], "clk_period": cp,
                          "fill_time": j0["fill_cyc"] * cp, "compute_time": j0["compute_cyc"] * cp,
                          "fill_cyc": j0["fill_cyc"], "compute_cyc": j0["compute_cyc"]})
    return db


def fit(points: list[dict], holdout: tuple[int, int]) -> dict:
    """Fit + save the two compute models on the (holdout-excluded) sweep; return a diagnostics dict."""
    train = [p for p in points if (p["n_row"], p["n_col"]) != holdout]
    db = corpus(train)
    fill = make_fill_model().fit(db)
    comp = make_compute_model().fit(db)
    fill.save_model()
    comp.save_model()

    # per-point predicted vs actual (cycles), incl. the holdout (a genuine test)
    def rows(model, tgt_cyc_key: str) -> list[dict]:
        out = []
        for p in points:
            j0 = p["per_job"][0]
            actual = j0.get(tgt_cyc_key)
            if actual is None:
                continue
            cp = float(p["clk_ns"]) * 1e-9
            pred_cyc = model.predict({"n_row": p["n_row"], "n_col": p["n_col"], "clk_period": cp}) / cp
            out.append({"point": [p["n_row"], p["n_col"]], "held_out": (p["n_row"], p["n_col"]) == holdout,
                        "pred": round(pred_cyc, 1), "actual": round(actual, 1),
                        "rel_err": round(abs(pred_cyc - actual) / actual, 4) if actual else None})
        return out

    return {"fill_coeffs": fill.coeffs, "compute_coeffs": comp.coeffs,
            "fill": rows(fill, "fill_cyc"), "compute": rows(comp, "compute_cyc")}


# --- Gate B: the emergent loaded sim (having just loaded the fitted models) vs the cosim sweep ----

def _sim_period(nr: int, nc: int, n_jobs: int = 4) -> float:
    from examples.rowwise_fir.fir_sim import FIRSim, make_specs
    sim = FIRSim(make_specs([(nr, nc)] * n_jobs, seed=0))
    sim.run()
    se = [next(e["cyc"] for e in sim.accel.events if e["event"] == "store_end" and e["tx_id"] == t)
          for t in range(n_jobs)]
    sp = [se[i + 1] - se[i] for i in range(n_jobs - 1)]
    return sp[len(sp) // 2 - 1]


def _sim_latency(nr: int, nc: int) -> float:
    from examples.rowwise_fir.fir_sim import FIRSim, make_specs
    sim = FIRSim(make_specs([(nr, nc)], seed=0))
    sim.run()

    def c(name: str) -> float:
        return next(e["cyc"] for e in sim.accel.events if e["event"] == name and e["tx_id"] == 0)

    return c("store_end") - c("cmd_arrive")


def validate(points: list[dict], holdout: tuple[int, int]) -> dict:
    rows = []
    for p in points:
        nr, nc = p["n_row"], p["n_col"]
        if p.get("period_cyc") is None or p.get("single_job_latency_cyc") is None:
            continue
        sp, sl = _sim_period(nr, nc), _sim_latency(nr, nc)
        rows.append({"point": [nr, nc], "held_out": (nr, nc) == holdout,
                     "period": {"sim": round(sp, 1), "cosim": p["period_cyc"],
                                "rel_err": round(abs(sp - p["period_cyc"]) / p["period_cyc"], 4)},
                     "latency": {"sim": round(sl, 1), "cosim": p["single_job_latency_cyc"],
                                 "rel_err": round(abs(sl - p["single_job_latency_cyc"])
                                                  / p["single_job_latency_cyc"], 4)}})
    return {"per_point": rows,
            "worst_period_rel_err": round(max(r["period"]["rel_err"] for r in rows), 4),
            "worst_latency_rel_err": round(max(r["latency"]["rel_err"] for r in rows), 4)}


def _print_fit(diag: dict) -> None:
    print(f"fill_model coeffs   : {diag['fill_coeffs']}")
    print(f"compute_model coeffs: {diag['compute_coeffs']}")
    for name in ("fill", "compute"):
        print(f"\n  {name}: pred vs actual (cyc)   [* = held out]")
        for r in diag[name]:
            star = " *" if r["held_out"] else ""
            print(f"    {str(r['point']):>10}  pred={r['pred']:>7}  actual={r['actual']:>7}  "
                  f"rel_err={r['rel_err']}{star}")


def main() -> None:
    data = json.loads(SWEEP_JSON.read_text(encoding="utf-8"))
    points = data["points"]
    holdout = tuple(data["holdout"])

    diag = fit(points, holdout)
    print("=== compute-model fit (contention-free per-job fill + store_span) ===")
    _print_fit(diag)

    val = validate(points, holdout)
    report = {"T": T, "clk_ns": points[0]["clk_ns"], "njobs": data["njobs"],
              "holdout": list(holdout), "fit": diag, "gate_b": val}
    REPORT_JSON.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    print("\n=== Gate B (loaded sim with the fitted models vs cosim) ===")
    print(f"worst period  rel_err: {val['worst_period_rel_err']*100:.1f}%")
    print(f"worst latency rel_err: {val['worst_latency_rel_err']*100:.1f}%")
    for r in val["per_point"]:
        star = " *held-out" if r["held_out"] else ""
        print(f"  {str(r['point']):>10}  period sim={r['period']['sim']:>7} cosim={r['period']['cosim']:>7}"
              f" ({r['period']['rel_err']*100:.1f}%)   lat sim={r['latency']['sim']:>7}"
              f" cosim={r['latency']['cosim']:>7} ({r['latency']['rel_err']*100:.1f}%){star}")
    print(f"\nwrote {REPORT_JSON}")


if __name__ == "__main__":
    main()
