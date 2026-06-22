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
from examples.rowwise_fir import fir_build  # noqa: E402
from examples.rowwise_fir.fir_golden import T  # noqa: E402

RESULTS = HERE / "results"
GRID_JSON = RESULTS / "cosim_grid.json"
CALIB_JSON = RESULTS / "fir_calibration.json"

# >=3 n_row x in-range n_col.  Holdout is INTERIOR (in the convex hull of the grid).
N_ROWS = [1, 2, 4, 8]
N_COLS = [64, 256, 1024]
HOLDOUT = (2, 256)            # interior held-out point (the verdict)
# Back-to-back: sim 2x(nr,nc) continuous == one RTL kernel of 2*nr rows (the per-row
# dataflow has no matrix boundary), so cosim(2*nr,nc) is the RTL reference.
BACKTOBACK = [(4, 64), (4, 256)]   # validated vs cosim(8,64), cosim(8,256) (in the grid)


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


def _feat(p, basis):
    f = {"1": 1.0, "n_row": float(p["n_row"]), "n_col": float(p["n_col"]),
         "trips": float(p["n_row"] * (p["n_col"] - T + 1)),
         "sqrt_nc": float(p["n_col"]) ** 0.5, "nr_sqrt_nc": p["n_row"] * float(p["n_col"]) ** 0.5}
    return [f[k] for k in basis]

# Per-span feature basis (the read is ~linear in words; the write gap + the first-row
# fill SATURATE in n_col -> a concave sqrt(n_col) term, else a linear-in-n_col model
# mispredicts at the n_col knee).  "1" is the intercept.
BASES = {
    "load":  ["1", "n_row", "n_col", "trips"],
    "store": ["1", "n_row", "n_col", "trips", "sqrt_nc", "nr_sqrt_nc"],
    "fill":  ["1", "n_row", "n_col", "trips", "sqrt_nc", "nr_sqrt_nc"],
}


def _fit(points, key, basis):
    from sklearn.linear_model import LinearRegression
    Xf = np.array([_feat(p, basis) for p in points])
    y = np.array([p[key] for p in points], dtype=float)
    m = LinearRegression(fit_intercept=False).fit(Xf, y)   # intercept is the "1" feature
    return {"basis": basis, "coeffs": [float(c) for c in m.coef_]}, float(m.score(Xf, y))


def _predict(model, p):
    return float(np.dot(model["coeffs"], _feat(p, model["basis"])))


def fit_and_validate() -> dict:
    grid = json.loads(GRID_JSON.read_text())["points"]
    fit_pts = [p for p in grid if (p["n_row"], p["n_col"]) != HOLDOUT]
    ho = next(p for p in grid if (p["n_row"], p["n_col"]) == HOLDOUT)
    # untrained-n_col held-out points (test the sqrt n_col interpolation honestly)
    ncol_ho_path = RESULTS / "cosim_holdout_ncol.json"
    ncol_ho = json.loads(ncol_ho_path.read_text())["points"] if ncol_ho_path.exists() else []

    keys = {"load": "x_read_span_cyc", "store": "y_write_span_cyc", "fill": "t_fill_cyc"}
    models, r2s = {}, {}
    for name, key in keys.items():
        models[name], r2s[name] = _fit(fit_pts, key, BASES[name])

    def residuals(p):
        out = {}
        for name, key in keys.items():
            pr = _predict(models[name], p)
            out[name] = {"pred": round(pr, 1), "actual": round(p[key], 1),
                         "rel_err": round(abs(pr - p[key]) / p[key], 4) if p[key] else None}
        whole_pred = _predict(models["fill"], p) + _predict(models["store"], p)
        out["whole_kernel"] = {"pred": round(whole_pred, 1), "actual": round(p["whole_kernel_cyc"], 1),
                               "rel_err": round(abs(whole_pred - p["whole_kernel_cyc"]) / p["whole_kernel_cyc"], 4)}
        return out

    calib = {
        "model": "span ~= sum(coeff_i * feature_i); features per-span (bilinear + concave sqrt(n_col) for store/fill)",
        "T": T, "clk_ns": grid[0]["clk_ns"],
        "fit_grid": [[p["n_row"], p["n_col"]] for p in fit_pts],
        "models": models,
        "r2": {k: round(v, 5) for k, v in r2s.items()},
        "holdout_interior": {"point": list(HOLDOUT), "note": "n_row interpolation (n_col=256 is a trained column)",
                             "residual": residuals(ho)},
        "holdout_ncol": [{"point": [p["n_row"], p["n_col"]],
                          "note": "UNTRAINED n_col (tests the sqrt n_col curve)",
                          "residual": residuals(p)} for p in ncol_ho],
    }
    CALIB_JSON.write_text(json.dumps(calib, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"r2": calib["r2"],
                      "holdout_interior_whole": calib["holdout_interior"]["residual"]["whole_kernel"],
                      "holdout_ncol_whole": [h["residual"]["whole_kernel"] for h in calib["holdout_ncol"]]}, indent=2))
    print(f"wrote {CALIB_JSON}")
    return calib


CLK_NS = 10.0


def _sim_events(specs):
    """Run the block-fidelity sim (loading the just-written calibration) and return events."""
    from examples.rowwise_fir.fir_sim import FIRSim, make_specs  # imports FIRTiming.from_calibration
    sim = FIRSim(make_specs(specs))
    sim.run()
    return sim.accel.events


def validate(calib: dict) -> dict:
    """Held-out single-command + back-to-back throughput, sim (fitted) vs cosim — the verdict."""
    grid = {(p["n_row"], p["n_col"]): p for p in json.loads(GRID_JSON.read_text())["points"]}
    cyc = lambda dt: dt / (CLK_NS * 1e-9)

    # held-out single command: per-event sim vs cosim (anchored at load_begin / X-read start)
    ev = {e["event"]: e["t"] for e in _sim_events([HOLDOUT]) if e["tx_id"] == 0}
    a = ev["cmd_arrive"]
    ho = grid[HOLDOUT]
    sim_single = {
        "x_read_span": cyc(ev["load_end"] - ev["load_begin"]),
        "y_write_span": cyc(ev["store_end"] - ev["store_begin"]),
        "t_fill": cyc(ev["store_begin"] - ev["load_begin"]),
        "whole_kernel": cyc(ev["store_end"] - a),
    }
    rel = lambda s, c: round(abs(s - c) / c, 4) if c else None
    single = {
        "point": list(HOLDOUT),
        "x_read_span": {"sim": round(sim_single["x_read_span"], 1), "cosim": round(ho["x_read_span_cyc"], 1),
                        "rel_err": rel(sim_single["x_read_span"], ho["x_read_span_cyc"])},
        "y_write_span": {"sim": round(sim_single["y_write_span"], 1), "cosim": round(ho["y_write_span_cyc"], 1),
                         "rel_err": rel(sim_single["y_write_span"], ho["y_write_span_cyc"])},
        "t_fill": {"sim": round(sim_single["t_fill"], 1), "cosim": round(ho["t_fill_cyc"], 1),
                   "rel_err": rel(sim_single["t_fill"], ho["t_fill_cyc"])},
        "whole_kernel": {"sim": round(sim_single["whole_kernel"], 1), "cosim": round(ho["whole_kernel_cyc"], 1),
                         "rel_err": rel(sim_single["whole_kernel"], ho["whole_kernel_cyc"])},
    }

    # back-to-back: sim 2x(nr,nc) span (load1_begin -> store2_end) vs cosim(2*nr,nc) whole-kernel
    b2b = []
    for nr, nc in BACKTOBACK:
        evs = _sim_events([(nr, nc), (nr, nc)])
        lb = min(e["t"] for e in evs if e["event"] == "load_begin")
        se = max(e["t"] for e in evs if e["event"] == "store_end")
        sim_span = cyc(se - lb)
        ref = grid[(2 * nr, nc)]["whole_kernel_cyc"]
        b2b.append({"sim_2x": [nr, nc], "cosim_ref": [2 * nr, nc],
                    "sim_span_cyc": round(sim_span, 1), "cosim_whole_cyc": round(ref, 1),
                    "rel_err": rel(sim_span, ref)})

    return {"single_holdout": single, "back_to_back": b2b}


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
