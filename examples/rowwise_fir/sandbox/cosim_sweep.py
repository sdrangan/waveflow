"""cosim_sweep.py — RTL-cosim calibration sweep for the FIR sandbox.

Drives run.tcl (single-port solution) over a (n_row, n_col) grid, records the
measured RTL kernel latency in cycles per point, and fits the refined cycle model
from plans/dataflow_composition.md:

    latency_cycles ~= L0 + n_row * L_row + II * trips,
        trips = n_row * (n_col - T + 1)        (output elements)

`L_row` captures the per-row burst setup + dataflow handshake (dominant at small
n_col); `II` is the steady-state cycles per output once rows are long enough to
amortize L_row.  One grid point is held out (n_row=4, n_col=1024) and its
predicted-vs-actual relative error is reported (vmac's held-out discipline).

It also records, at a couple of sizes, the SPLIT-bundle latency alongside the
single-port latency.  These come out identical: a single m_axi bundle is
full-duplex (independent read/write channels), so the X-read + Y-write pair does
not contend, and splitting X/Y onto separate bundles changes nothing.  This is
the corrected finding (the plan's "single-port read+write = II=2" premise does
not hold; the #streams->II floor applies to *same-direction* streams, e.g.
VMAC's two interleaved reads).

Emits results/cosim_sweep.json in the shape of examples/vmac/timeline/sim_sweep.json.

Run with the project venv (needs Vitis HLS + Vivado xsim)::

    PYTHONPATH=../../.. ../../../pysilicon-venv/Scripts/python.exe cosim_sweep.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(REPO))

from waveflow.toolchain import toolchain  # noqa: E402
from waveflow.utils.cosimparse import CosimReportParser  # noqa: E402

import gen_data  # noqa: E402  (sibling module)

T = gen_data.T
CLK_NS = 10.0

# Fit grid: 2 row counts x 6 column counts = 12 points spanning trip counts.
FIT_GRID = [(nr, nc) for nr in (1, 4) for nc in (16, 32, 64, 128, 256, 512)]
HOLDOUT = (4, 1024)
# Sizes at which we ALSO cosim the split-bundle solution (full-duplex evidence).
DUPLEX_POINTS = {(1, 64), (4, 256)}

SWEEP_DIR = HERE / "sweep"
RESULTS = HERE / "results"
SINGLE_SOL = HERE / "waveflow_rowwise_fir_proj" / "solution_singleport"
SPLIT_SOL = HERE / "waveflow_rowwise_fir_split_proj" / "solution_split"


def _cosim_cycles(sol_path: Path) -> int:
    cyc = CosimReportParser(sol_path=str(sol_path), top="fir_accel").get_transaction_cycles()
    if cyc is None:
        raise RuntimeError(f"no cosim cycles parsed from {sol_path}")
    return int(cyc)


def run_point(n_row: int, n_col: int, want_split: bool) -> dict:
    tag = f"{n_row}x{n_col}"
    data_dir = SWEEP_DIR / tag / "data"
    gen_data.write_fixture(data_dir, n_row, n_col, seed=n_row * 1000 + n_col)

    env = {
        "WAVEFLOW_ROWWISE_FIR_COSIM": "1",
        # Forward slashes: backslashes get mangled through the cmd.exe call layer.
        "WAVEFLOW_ROWWISE_FIR_DATA_DIR": data_dir.as_posix(),
        "WAVEFLOW_ROWWISE_FIR_SINGLEPORT_ONLY": "0" if want_split else "1",
    }
    # xsim cosim is occasionally flaky on the first launch; retry once.
    last = ""
    for attempt in (1, 2):
        print(f"[{tag}] cosim (split={want_split}) attempt {attempt} ...", flush=True)
        res = toolchain.run_vitis_hls_result(HERE / "run.tcl", work_dir=HERE,
                                             capture_output=True, env=env)
        last = (res.get("stdout") or "") + (res.get("stderr") or "")
        if "WAVEFLOW_SUCCESS" in last:
            break
    if "WAVEFLOW_SUCCESS" not in last:
        raise RuntimeError(f"[{tag}] run.tcl did not report success.\n--- tail ---\n{last[-2500:]}")

    trips = n_row * (n_col - T + 1)
    single = _cosim_cycles(SINGLE_SOL)
    point = {
        "n_rows": n_row,
        "n_cols": n_col,
        "trips": trips,
        "rtl_cycles": single,
        "rtl_latency_ns": single * CLK_NS,
        "held_out": (n_row, n_col) == HOLDOUT,
    }
    if want_split:
        point["rtl_cycles_split"] = _cosim_cycles(SPLIT_SOL)
    msg = f"[{tag}] trips={trips} rtl_cycles={single}"
    if want_split:
        msg += f" split={point['rtl_cycles_split']} (identical={single == point['rtl_cycles_split']})"
    print(msg, flush=True)
    return point


def fit_model(points: list[dict]) -> dict:
    """Fit the cycle model and characterize steady-state throughput.

    The plan's form ``L0 + n_row*L_row + II*trips`` (single global II) does NOT
    describe this per-row-DATAFLOW kernel: cross-row overlap makes the per-row
    interval scale with n_col, so latency is *bilinear* in (n_row, n_col).  We
    therefore fit the bilinear refinement

        latency_cycles ~= L0 + L_row*n_row + L_col*n_col + II*trips

    (an ``L_col*n_col`` pipeline-fill term added to the plan's form), and also
    report the directly-measured steady-state throughput (cycles/output as
    n_col, n_row grow), which is the number a coarse `block` model should use.
    """
    fit = [p for p in points if not p["held_out"]]

    def design(p):
        return [1.0, p["n_rows"], p["n_cols"], p["trips"]]

    A = np.array([design(p) for p in fit])
    y = np.array([p["rtl_cycles"] for p in fit], dtype=float)
    coef, *_ = np.linalg.lstsq(A, y, rcond=None)
    L0, L_row, L_col, II = (float(c) for c in coef)
    pred = A @ coef
    ss_res = float(np.sum((y - pred) ** 2))
    ss_tot = float(np.sum((y - y.mean()) ** 2))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else float("nan")

    # Measured steady-state throughput: slope between the two largest-trip
    # same-n_row points (overhead amortized).
    big = sorted([p for p in points if p["n_rows"] == max(q["n_rows"] for q in points)],
                 key=lambda p: p["trips"])[-2:]
    asymptote = ((big[-1]["rtl_cycles"] - big[0]["rtl_cycles"]) /
                 (big[-1]["trips"] - big[0]["trips"])) if len(big) == 2 else None

    calib = {
        "model": "latency_cycles ~= L0 + L_row*n_row + L_col*n_col + II*trips",
        "model_note": (
            "Bilinear: the plan's L0 + n_row*L_row + II*trips form (single global "
            "II~=2) does NOT fit -- per-row DATAFLOW overlap makes the per-row "
            "interval scale with n_col, so an L_col*n_col pipeline-fill term is "
            "required.  There is no single 'II=2' floor; see steady_state_cyc_per_output."
        ),
        "trips_formula": "trips = n_row * (n_col - T + 1)",
        "L0": round(L0, 4),
        "L_row": round(L_row, 4),
        "L_col": round(L_col, 4),
        "ii": round(II, 4),
        "r2": round(float(r2), 6),
        "steady_state_cyc_per_output": round(asymptote, 4) if asymptote else None,
        "fit_configs": [[p["n_rows"], p["n_cols"]] for p in fit],
    }
    ho = next((p for p in points if p["held_out"]), None)
    if ho is not None:
        ho_pred = float(np.array(design(ho)) @ coef)
        calib.update(
            {
                "holdout": [ho["n_rows"], ho["n_cols"]],
                "holdout_trips": ho["trips"],
                "holdout_actual_cycles": ho["rtl_cycles"],
                "holdout_pred_cycles": round(ho_pred, 2),
                "holdout_rel_err": round(abs(ho_pred - ho["rtl_cycles"]) / ho["rtl_cycles"], 5),
                "holdout_note": (
                    "n_col=1024 is a 2x extrapolation beyond the fit grid (max 512) "
                    "into the burst-amortized regime where cyc/output is still "
                    "decreasing, so the affine fit overshoots."
                ),
            }
        )
    return calib


def emit(points: list[dict]) -> None:
    calib = fit_model(points)
    artifact = {
        "source": "cosim",
        "tool": "Vitis HLS 2025.1 cosim (xsim), xc7z020clg484-1, create_clock -period 10",
        "timebase": "ns",
        "clk_period_ns": CLK_NS,
        "T": T,
        "calibration": calib,
        "fullduplex_finding": (
            "single-port (X+Y share bundle=gmem) and split (gmemX/gmemY) cosim to "
            "identical cycles; a single m_axi bundle is full-duplex, so a read+write "
            "pair does not contend. See rtl_cycles_split on the duplex-evidence points."
        ),
        "points": sorted(points, key=lambda p: (p["n_rows"], p["n_cols"])),
    }
    out = RESULTS / "cosim_sweep.json"
    out.write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"\nwrote {out}")
    print(f"calibration: {json.dumps(calib, indent=2)}")


def main() -> None:
    RESULTS.mkdir(parents=True, exist_ok=True)
    if "--refit" in sys.argv:
        # Re-fit from the committed points without re-running cosim.
        existing = json.loads((RESULTS / "cosim_sweep.json").read_text())
        emit(existing["points"])
        return
    points = []
    for n_row, n_col in FIT_GRID + [HOLDOUT]:
        points.append(run_point(n_row, n_col, want_split=(n_row, n_col) in DUPLEX_POINTS))
    emit(points)


if __name__ == "__main__":
    main()
