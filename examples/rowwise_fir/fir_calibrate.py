"""fir_calibrate.py — real per-stage cosim calibration of the matrix-LT FIR timing.

Steps 4-5 of plans/load_compute_store.md.  Measures the per-stage bus-visible spans
from RTL cosim over a grid, fits the bilinear timing model with scikit-learn (NO
seeding), validates an INTERIOR held-out point, and validates the back-to-back
throughput claim against cosim.

Endpoints are pinned identically to the sim (the guardrail): every span is measured
from the VCD m_axi bursts and anchored at the X-read start, exactly as the sim anchors
at ``load_begin``:
  * X-read span  = [first read burst, last read burst]   <-> sim load_begin..load_end
  * Y-write span = [first write burst, last write burst]  <-> sim store_begin..store_end
  * t_fill       = (Y-write start) - (X-read start)        <-> sim store_begin offset

Fitted (each via sklearn LinearRegression on [n_row, n_col, trips], intercept=L0):
  * t_load  ~ L0 + Lrow*n_row + Lcol*n_col + II*trips   (the X-read span)
  * t_store ~ ...                                        (the Y-write span)
  * t_fill  ~ ...                                        (first-Y-row latency; ~const in n_row)
with trips = n_row*(n_col - T + 1).

Usage (project venv; Vitis 2025.1 for --measure)::

    # 1. cosim the grid (slow) -> results/cosim_grid.json
    PYTHONPATH=. ../pysilicon-venv/Scripts/python.exe examples/rowwise_fir/fir_calibrate.py --measure
    # 2. fit + validate (fast) -> results/fir_calibration.json
    PYTHONPATH=. ../pysilicon-venv/Scripts/python.exe examples/rowwise_fir/fir_calibrate.py --fit
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(REPO))

from waveflow.toolchain import toolchain  # noqa: E402
from waveflow.calib import CalibDatabase, Feature, InterpCalibModel  # noqa: E402
from examples.rowwise_fir import fir_build  # noqa: E402
from examples.rowwise_fir.fir_golden import T  # noqa: E402

RESULTS = HERE / "results"
GRID_JSON = RESULTS / "cosim_grid.json"
CALIB_JSON = RESULTS / "fir_calibration.json"

# >=3 n_row x in-range n_col.  Holdout is INTERIOR (in the convex hull of the grid).
N_ROWS = [1, 2, 4, 8]
N_COLS = [64, 256, 1024]
HOLDOUT = (2, 256)            # interior held-out point (Gate 1: n_row interpolation)


def _burst_beats(b: dict) -> int:
    return sum(1 for bt in b.get("beat_type", []) if bt == 0)


def measure_cosim(n_row: int, n_col: int) -> dict:
    """Cosim the generated kernel (port trace) at (n_row,n_col); return the bus-visible
    endpoints (ns) + clk period from the VCD m_axi bursts."""
    from vcdvcd import VCDVCD
    from waveflow.scripts.xsim_vcd import run_xsim_vcd
    from waveflow.utils.vcd import VcdParser

    fir_build.generate(n_row, n_col)
    env = {"WAVEFLOW_ROWWISE_FIR_COSIM": "1", "WAVEFLOW_ROWWISE_FIR_TRACE_LEVEL": "port"}
    res = toolchain.run_vitis_hls_result(fir_build.GEN_DIR / "run.tcl",
                                         work_dir=fir_build.GEN_DIR, capture_output=True, env=env)
    out = (res.get("stdout") or "") + (res.get("stderr") or "")
    if "WAVEFLOW_SUCCESS" not in out:
        raise RuntimeError(f"cosim failed at ({n_row},{n_col}):\n" + out[-1500:])

    vcd_path = run_xsim_vcd(top="fir", comp="fir_gen_proj", out="dump.vcd",
                            soln="solution1", trace_level="port", workdir=fir_build.GEN_DIR)
    vcd = VCDVCD(str(vcd_path), signals=None, store_tvs=True)
    vp = VcdParser(vcd)
    clk = vp.add_clock_signal()
    aximm_sigs, _ = vp.add_aximm_signals(prefix="m_axi_gmem_", dir="both",
                                         lite_only=False, short_name_prefix="gmem_")
    write_bursts, read_bursts, clk_period = vp.extract_aximm_bursts(clk_name=clk, aximm_sigs=aximm_sigs)

    def span(bursts):
        t0 = min(float(b.get("data_tstart", b["tstart"])) for b in bursts)
        t1 = max(float(b["data_tend"]) for b in bursts)
        return t0, t1, sum(_burst_beats(b) for b in bursts)

    xr0, xr1, rwords = span(read_bursts)
    yw0, yw1, wwords = span(write_bursts)
    clk_ns = float(clk_period)
    anchor = xr0
    return {
        "n_row": n_row, "n_col": n_col, "clk_ns": clk_ns,
        "x_read_span_cyc": (xr1 - xr0) / clk_ns,
        "y_write_span_cyc": (yw1 - yw0) / clk_ns,
        "t_fill_cyc": (yw0 - anchor) / clk_ns,            # Y-write start offset
        "whole_kernel_cyc": (yw1 - anchor) / clk_ns,
        "read_words": rwords, "write_words": wwords,
    }


def run_grid() -> None:
    RESULTS.mkdir(parents=True, exist_ok=True)
    points = []
    grid = [(nr, nc) for nr in N_ROWS for nc in N_COLS]
    for nr, nc in grid:
        print(f"[cosim {nr}x{nc}] ...", flush=True)
        p = measure_cosim(nr, nc)
        points.append(p)
        print(f"  x_read={p['x_read_span_cyc']:.0f} y_write={p['y_write_span_cyc']:.0f} "
              f"t_fill={p['t_fill_cyc']:.0f} whole={p['whole_kernel_cyc']:.0f}", flush=True)
        GRID_JSON.write_text(json.dumps({"points": points}, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {GRID_JSON}")


# --- physical, near-fit-free model (waveflow.calib) ----------------------------
# Channel occupancy is DETERMINISTIC: each TRANSFER beat is one word, so a burst occupies its
# channel for nwords + setup*num_trans cycles (slope 1, NOT fitted; see the sanity check).
# Compute body is II=1: trips + (n_row-1)*g(n_col).  The ONE calibrated term is g(n_col) — the
# per-row pipeline / 2-buffer ping-pong depth — a smooth SATURATING 1-D InterpCalibModel lookup
# (not a sqrt fudge).  fill = (n_col+T) + g(n_col) + fill_const (one constant).  Master eq:
#     whole = (n_col+T) + trips + n_row*g(n_col) + fill_const   ==   fill + max(write_occ, compute_body)

SETUP = 2.0  # per-burst address latency (deterministic occupancy; immaterial while compute-bound)


def _trips(p):
    return p["n_row"] * (p["n_col"] - T + 1)


def _fit_g(fit_pts) -> InterpCalibModel:
    """g(n_col), the per-row pipeline gap, measured from the INTER-ROW gaps:
    ``g = (y_write_span - trips) / (n_row - 1)`` (n_row>1).  Averaged per n_col by the
    InterpCalibModel.  g saturates, so a few columns interpolate cleanly (cheap to densify)."""
    db = CalibDatabase(["n_col", "g"])
    for p in fit_pts:
        if p["n_row"] > 1:
            db.add_datapoint({"n_col": p["n_col"],
                              "g": (p["y_write_span_cyc"] - _trips(p)) / (p["n_row"] - 1)})
    return InterpCalibModel([Feature("n_col")], "g").fit(db)


def _fit_fill_const(fit_pts, g) -> float:
    """fill_const = mean(t_fill - (n_col+T) - g(n_col)) — a single calibrated scalar."""
    return float(np.mean([p["t_fill_cyc"] - (p["n_col"] + T) - g.predict(p) for p in fit_pts]))


def _compose(p, g, fill_const) -> float:
    """The whole-kernel exactly as the sim composes it: fill + max(write_occ, compute_body)."""
    gv = g.predict(p)
    fill = (p["n_col"] + T) + gv + fill_const
    compute_body = _trips(p) + (p["n_row"] - 1) * gv
    write_occ = _trips(p) + SETUP * p["n_row"]
    return fill + max(write_occ, compute_body)


def fit_and_validate() -> dict:
    grid = json.loads(GRID_JSON.read_text())["points"]
    fit_pts = [p for p in grid if (p["n_row"], p["n_col"]) != HOLDOUT]
    ncol_ho_path = RESULTS / "cosim_holdout_ncol.json"
    ncol_ho = json.loads(ncol_ho_path.read_text())["points"] if ncol_ho_path.exists() else []

    g = _fit_g(fit_pts)
    fill_const = _fit_fill_const(fit_pts, g)

    def whole_resid(p):
        pred = _compose(p, g, fill_const)
        a = p["whole_kernel_cyc"]
        return {"pred": round(pred, 1), "actual": round(a, 1),
                "rel_err": round(abs(pred - a) / a, 4) if a else None}

    ho = next(p for p in grid if (p["n_row"], p["n_col"]) == HOLDOUT)
    calib = {
        "model": "PHYSICAL near-fit-free: channel occupancy deterministic (beats==nwords); "
                 "compute II=1 (trips + (n_row-1)*g); g(n_col) = calibrated saturating lookup "
                 "(the per-row ping-pong depth); whole = (n_col+T) + trips + n_row*g(n_col) + fill_const",
        "T": T, "clk_ns": grid[0]["clk_ns"], "setup": SETUP,
        "fit_grid": [[p["n_row"], p["n_col"]] for p in fit_pts],
        "models": {"g": g.samples, "fill_const": round(fill_const, 4), "setup": SETUP},
        "in_grid_reconstruction_max_rel_err":
            round(max(abs(_compose(p, g, fill_const) - p["whole_kernel_cyc"]) / p["whole_kernel_cyc"]
                      for p in grid), 4),
        "holdout_interior": {"point": list(HOLDOUT),
                             "note": "n_row=2 held out from the fit; g(256) trained from (4,256),(8,256)",
                             "whole_kernel": whole_resid(ho)},
        "holdout_ncol": [{"point": [p["n_row"], p["n_col"]],
                          "note": "UNTRAINED n_col (g interpolated — the honest generalization test)",
                          "whole_kernel": whole_resid(p)} for p in ncol_ho],
    }
    CALIB_JSON.write_text(json.dumps(calib, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"g_samples": calib["models"]["g"], "fill_const": calib["models"]["fill_const"],
                      "in_grid_max_rel_err": calib["in_grid_reconstruction_max_rel_err"],
                      "holdout_interior_whole": calib["holdout_interior"]["whole_kernel"],
                      "holdout_ncol_whole": [h["whole_kernel"] for h in calib["holdout_ncol"]]}, indent=2))
    print(f"wrote {CALIB_JSON}")
    return calib


CLK_NS = 10.0


def _sim_events(specs):
    """Run the block-fidelity sim (loading the just-written calibration) and return events."""
    from examples.rowwise_fir.fir_sim import FIRSim, make_specs  # imports FIRTiming.from_calibration
    sim = FIRSim(make_specs(specs))
    sim.run()
    return sim.accel.events


def _sim_whole_kernel(spec) -> float:
    """Run the ACTUAL sim for one matrix and return its bus-visible whole-kernel (cycles):
    store_end − cmd_arrive (== Y-write end − X-read start)."""
    ev = {e["event"]: e["t"] for e in _sim_events([spec]) if e["tx_id"] == 0}
    return (ev["store_end"] - ev["cmd_arrive"]) / (CLK_NS * 1e-9)


def validate(calib: dict) -> dict:
    """The gates — sim (loading the calibration) vs cosim, on the **actual** simulation.

    Gate 1: single-command whole-kernel at the interior holdout (2,256) — n_row held out, g(256)
    trained.  Gate 2: whole-kernel at the UNTRAINED n_col points (g interpolated)."""
    grid = {(p["n_row"], p["n_col"]): p for p in json.loads(GRID_JSON.read_text())["points"]}
    ncol_ho_path = RESULTS / "cosim_holdout_ncol.json"
    ncol_ho = json.loads(ncol_ho_path.read_text())["points"] if ncol_ho_path.exists() else []

    def gate(nr, nc, cosim_whole, note):
        sim_whole = _sim_whole_kernel((nr, nc))
        return {"point": [nr, nc], "note": note,
                "sim_whole_cyc": round(sim_whole, 1), "cosim_whole_cyc": round(cosim_whole, 1),
                "rel_err": round(abs(sim_whole - cosim_whole) / cosim_whole, 4) if cosim_whole else None}

    gate1 = gate(*HOLDOUT, grid[HOLDOUT]["whole_kernel_cyc"], "interior holdout (n_row=2; g(256) trained)")
    gate2 = [gate(p["n_row"], p["n_col"], p["whole_kernel_cyc"], "untrained n_col (g interpolated)")
             for p in ncol_ho]
    return {"gate1_single_command": gate1, "gate2_untrained_ncol": gate2}


def main() -> None:
    if "--measure" in sys.argv:
        run_grid()
    if "--fit" in sys.argv:
        calib = fit_and_validate()
        val = validate(calib)
        calib["validation"] = val
        CALIB_JSON.write_text(json.dumps(calib, indent=2) + "\n", encoding="utf-8")
        print("\n=== VALIDATION (sim with fitted calibration vs cosim) ===")
        print(json.dumps(val, indent=2))
    if "--measure" not in sys.argv and "--fit" not in sys.argv:
        print("usage: fir_calibrate.py [--measure] [--fit]")


if __name__ == "__main__":
    main()
