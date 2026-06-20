"""``vmac_calibrate`` — fit the II-decoupled timing model from the committed cosim sweep.

Stage 5 of ``plans/vmac_mm_queue_timing.md``.  Reads ONLY the committed calibration artifact
``calibration/vmac_calibration.json`` (no Vitis), fits the pipeline-schedule model

    latency_cycles ~= depth + II * trips,    trips = n_rows * ceil(n_cols / PF)

on a SUBSET of the swept configs and reports the prediction error on the HELD-OUT config(s) —
the whole point of the methodology: a single-point fit is tautological; a swept fit that
predicts un-fit sizes is real calibration.

Two consumers read the fit:
  * :func:`fitted_params` returns ``(depth, II)`` for the II-decoupled timing model in
    ``vmac.py`` (loaded programmatically — no magic constants in the model).
  * ``__main__`` prints the fit table + held-out error (the report this stage hands back).
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

CALIB_JSON = Path(__file__).resolve().parent / "calibration" / "vmac_calibration.json"

# Hold out the LARGEST config (8x16, trips=128): fit the 3 smaller, predict the largest.
# Extrapolating to the biggest trip count is the strongest generalization test — if (depth, II)
# are stable, an extrapolated prediction lands; a single-point fit could never be checked at all.
HOLDOUT = (8, 16)


@dataclass(frozen=True)
class Fit:
    depth: float
    ii: float
    fit_configs: list[tuple[int, int]]
    holdout: tuple[int, int]
    holdout_trips: int
    holdout_actual: int
    holdout_pred: float
    holdout_abs_err: float
    holdout_rel_err: float
    r2: float  # goodness of the linear fit on the fit subset

    def predict(self, trips: int) -> float:
        return self.depth + self.ii * trips


def _trips(n_rows: int, n_cols: int, pf: int) -> int:
    return n_rows * math.ceil(n_cols / pf)


def _linfit(trips: list[float], cycles: list[float]) -> tuple[float, float, float]:
    """Ordinary least squares ``cycles = depth + II*trips``; returns (depth, II, R^2)."""
    n = len(trips)
    sx = sum(trips)
    sy = sum(cycles)
    sxx = sum(t * t for t in trips)
    sxy = sum(t * c for t, c in zip(trips, cycles))
    denom = n * sxx - sx * sx
    if denom == 0:
        raise ValueError("degenerate fit: all trip counts equal; sweep must span distinct trips.")
    ii = (n * sxy - sx * sy) / denom
    depth = (sy - ii * sx) / n
    mean_y = sy / n
    ss_tot = sum((c - mean_y) ** 2 for c in cycles)
    ss_res = sum((c - (depth + ii * t)) ** 2 for t, c in zip(trips, cycles))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 1.0
    return depth, ii, r2


def load_sweep() -> dict:
    if not CALIB_JSON.exists():
        raise FileNotFoundError(
            f"calibration artifact missing: {CALIB_JSON}\n"
            "run examples/vmac/vmac_cosim_sweep.py (needs Vitis) to produce it."
        )
    return json.loads(CALIB_JSON.read_text(encoding="utf-8"))


def fit(holdout: tuple[int, int] = HOLDOUT) -> Fit:
    """Fit ``(depth, II)`` on every swept config except *holdout*; validate on *holdout*."""
    art = load_sweep()
    pts = {(p["n_rows"], p["n_cols"]): p for p in art["sweep"]}
    if holdout not in pts:
        raise ValueError(f"holdout {holdout} not in sweep {sorted(pts)}")
    fit_keys = [k for k in pts if k != holdout]
    trips = [float(pts[k]["trips"]) for k in fit_keys]
    cycles = [float(pts[k]["transaction_cycles"]) for k in fit_keys]
    depth, ii, r2 = _linfit(trips, cycles)

    h = pts[holdout]
    h_trips = int(h["trips"])
    h_actual = int(h["transaction_cycles"])
    h_pred = depth + ii * h_trips
    abs_err = abs(h_pred - h_actual)
    rel_err = abs_err / h_actual if h_actual else float("nan")
    return Fit(
        depth=depth, ii=ii, fit_configs=fit_keys, holdout=holdout,
        holdout_trips=h_trips, holdout_actual=h_actual, holdout_pred=h_pred,
        holdout_abs_err=abs_err, holdout_rel_err=rel_err, r2=r2,
    )


def fitted_params() -> tuple[float, float]:
    """``(depth, II)`` for the II-decoupled timing model — fit on the held-out subset.

    The timing model uses exactly the parameters validated against the held-out config, so the
    sim's absolute latency is the *predicted* (not the fit-to) cycle count at every size."""
    f = fit()
    return f.depth, f.ii


def _loo_errors() -> list[dict]:
    """Leave-one-out: for completeness, fit on N-1 and predict each held-out point in turn."""
    art = load_sweep()
    keys = [(p["n_rows"], p["n_cols"]) for p in art["sweep"]]
    rows = []
    for k in keys:
        f = fit(holdout=k)
        rows.append({
            "holdout": k, "trips": f.holdout_trips, "actual": f.holdout_actual,
            "pred": f.holdout_pred, "rel_err": f.holdout_rel_err,
        })
    return rows


def main() -> None:
    art = load_sweep()
    print("=== VMAC II-decoupling calibration (Stage 5) ===")
    print(f"artifact : {CALIB_JSON}")
    print(f"model    : {art['latency_model']}   ({art['trips_formula']}, PF={art['pf']})")
    print(f"clk      : {art['clk_period_ns']} ns/cycle\n")
    print("swept cosim points (RTL ground truth):")
    print(f"  {'size':>7} {'trips':>6} {'rtl_cycles':>11} {'read_words':>11} {'vcd_ns':>9}")
    for p in art["sweep"]:
        print(f"  {p['n_rows']:>3}x{p['n_cols']:<3} {p['trips']:>6} "
              f"{p['transaction_cycles']:>11} {p['read_words']:>11} {p['vcd_latency_ns']:>9.1f}")

    f = fit()
    fit_sizes = ", ".join(f"{r}x{c}" for r, c in f.fit_configs)
    print(f"\nfit on {{{fit_sizes}}}  (held out {f.holdout[0]}x{f.holdout[1]}):")
    print(f"  depth = {f.depth:.2f} cycles,  II = {f.ii:.3f} cycles/trip,  R^2 = {f.r2:.5f}")
    print(f"\nHELD-OUT validation ({f.holdout[0]}x{f.holdout[1]}, trips={f.holdout_trips}):")
    print(f"  predicted = {f.holdout_pred:.1f} cycles,  actual = {f.holdout_actual} cycles")
    print(f"  abs err = {f.holdout_abs_err:.1f} cycles,  REL ERR = {f.holdout_rel_err * 100:.2f}%")

    print("\nleave-one-out (each size predicted from the other three):")
    print(f"  {'holdout':>8} {'trips':>6} {'actual':>7} {'pred':>8} {'rel_err':>8}")
    for r in _loo_errors():
        print(f"  {r['holdout'][0]:>3}x{r['holdout'][1]:<3} {r['trips']:>6} {r['actual']:>7} "
              f"{r['pred']:>8.1f} {r['rel_err'] * 100:>7.2f}%")


if __name__ == "__main__":
    main()
