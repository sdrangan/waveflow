"""Committed-figure workflow for the rowwise-FIR cosim-calibrated timing example.

Renders ONE deterministic SVG telling the **physical, near-fit-free calibration** story: the
matrix-LT FIR whole-kernel latency decomposes into deterministic channel occupancy + an exact
II=1 compute + a single fitted curve ``row_depth(n_col)`` (the per-row ping-pong refill depth):

    whole = (n_col + T) + trips + n_row * row_depth(n_col) + fill_const,   trips = n_row*(n_col-T+1)

The figure regenerates from **committed JSONs ALONE** — no Vitis, no cosim re-run, no VCD:

    examples/rowwise_fir/results/cosim_grid.json     (measured whole-kernel per (n_row, n_col))
    examples/rowwise_fir/results/fir_calibration.json (the fitted row_depth lookup + fill_const)

Two panels:
  (a) predicted (the model) vs measured (cosim) whole-kernel across the 12-point grid + the two
      untrained-n_col held-out points — all on the y=x line (worst <= 1.3%), the generalization test.
  (b) the ONE fitted curve, row_depth(n_col): three measured samples + the interpolating lookup,
      saturating at the FIR pipeline depth (~268 cyc).

Run::

    PYTHONPATH=. ../pysilicon-venv/Scripts/python.exe examples/rowwise_fir/fir_figures.py          # regenerate
    PYTHONPATH=. ../pysilicon-venv/Scripts/python.exe examples/rowwise_fir/fir_figures.py --check  # byte-match
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

import numpy as np
import matplotlib

matplotlib.use("svg")
# Deterministic SVG: a stable hashsalt fixes the element ids matplotlib would otherwise randomize,
# so a re-render only diffs when the figure truly changed (mirrors vmac_figures / shared_mem_figures).
matplotlib.rcParams["svg.hashsalt"] = "rowwise_fir_figures"
import matplotlib.pyplot as plt  # noqa: E402

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parents[1]
RESULTS = _HERE / "results"
GRID_JSON = RESULTS / "cosim_grid.json"
CALIB_JSON = RESULTS / "fir_calibration.json"
COMMITTED_SVG = _REPO / "docs" / "examples" / "rowwise_fir" / "images" / "sim_vs_cosim.svg"
SYNC_STATUS = COMMITTED_SVG.with_name("sync_status.json")

_MEAS_C, _PRED_C, _HOLD_C = "#4C78A8", "#333333", "#E45756"
_RD_C = "#54A24B"


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _save_svg(fig, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # metadata Date=None omits the <dc:date> matplotlib embeds by default (else non-deterministic).
    fig.savefig(path, format="svg", bbox_inches="tight", metadata={"Date": None})
    plt.close(fig)


def _sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _predict_whole(n_row, n_col, calib) -> float:
    """Reconstruct the sim's whole-kernel exactly as FIRTiming composes it."""
    T = calib["T"]
    rd = calib["models"]["row_depth"]
    row_depth = float(np.interp(n_col, rd["x"], rd["y"]))   # clamped past the ends == saturation
    trips = n_row * (n_col - T + 1)
    return (n_col + T) + trips + n_row * row_depth + calib["models"]["fill_const"]


# ---------------------------------------------------------------------------
# Rendering (reads only the committed JSONs)
# ---------------------------------------------------------------------------

def render(out_svg: Path) -> Path:
    grid = _load(GRID_JSON)["points"]
    calib = _load(CALIB_JSON)

    # in-grid points: pred from the model, measured from cosim
    meas = [p["whole_kernel_cyc"] for p in grid]
    pred = [_predict_whole(p["n_row"], p["n_col"], calib) for p in grid]
    # the two untrained-n_col held-out points (row_depth interpolated — the generalization test)
    hold = calib["validation"]["gate2_untrained_ncol"]
    h_meas = [h["cosim_whole_cyc"] for h in hold]
    h_pred = [h["sim_whole_cyc"] for h in hold]
    h_lab = [f"{h['point'][0]}x{h['point'][1]}" for h in hold]
    worst = max(abs(pr - me) / me for pr, me in zip(pred, meas)) * 100

    fig = plt.figure(figsize=(10.4, 4.6))
    gs = fig.add_gridspec(1, 2, width_ratios=[1.05, 1.0], wspace=0.30)
    ax_par = fig.add_subplot(gs[0, 0])
    ax_rd = fig.add_subplot(gs[0, 1])

    # -- Panel (a): predicted vs measured whole-kernel (parity) -------------------
    lim = max(max(meas), max(pred)) * 1.07
    ax_par.plot([0, lim], [0, lim], color="#BBBBBB", lw=1.2, zorder=1, label="y = x (exact)")
    ax_par.scatter(meas, pred, s=46, color=_MEAS_C, edgecolor="white", lw=0.6, zorder=3,
                   label="12-point fit/grid")
    ax_par.scatter(h_meas, h_pred, s=110, facecolors="none", edgecolors=_HOLD_C, lw=1.8, zorder=4,
                   label="untrained n_col (held out)")
    for me, pr, lab in zip(h_meas, h_pred, h_lab):
        ax_par.annotate(lab, xy=(me, pr), xytext=(me + lim * 0.015, pr - lim * 0.05),
                        fontsize=8, color=_HOLD_C, ha="left")
    ax_par.set_xlim(0, lim)
    ax_par.set_ylim(0, lim)
    ax_par.set_aspect("equal")
    ax_par.set_xlabel("measured whole-kernel — RTL cosim [cyc]")
    ax_par.set_ylabel("predicted — physical LT model [cyc]")
    ax_par.set_title(f"(a) one fitted curve predicts the whole kernel\nworst {worst:.1f}% over the "
                     f"grid; untrained n_col within {max(h['rel_err'] for h in hold) * 100:.1f}%",
                     fontsize=9.5)
    ax_par.legend(fontsize=8, loc="upper left", framealpha=0.9)
    ax_par.spines[["top", "right"]].set_visible(False)
    ax_par.grid(color="#EEEEEE", zorder=0)

    # -- Panel (b): the one fitted curve, row_depth(n_col) ------------------------
    rd = calib["models"]["row_depth"]
    xs = np.array(rd["x"])
    ys = np.array(rd["y"])
    xx = np.linspace(0, xs[-1] * 1.05, 400)
    yy = np.interp(xx, xs, ys)
    ax_rd.plot(xx, yy, color=_RD_C, lw=2.0, zorder=2, label="InterpCalibModel lookup")
    ax_rd.scatter(xs, ys, s=70, color=_RD_C, edgecolor="white", lw=0.8, zorder=3,
                  label="cosim samples")
    for x, y in zip(xs, ys):
        ax_rd.annotate(f"{y:.0f}", xy=(x, y), xytext=(x, y - 22), fontsize=8, color="#333",
                       ha="center")
    ax_rd.axhline(ys[-1], color="#CCCCCC", lw=1.0, ls="--", zorder=1)
    ax_rd.text(xs[-1] * 0.5, ys[-1] + 6, f"saturates ≈ {ys[-1]:.0f} cyc (FIR pipeline depth)",
               fontsize=8, color="#777", ha="center")
    ax_rd.set_xlabel("n_col (row length)")
    ax_rd.set_ylabel("row_depth [cyc]")
    ax_rd.set_ylim(0, ys[-1] * 1.28)
    ax_rd.set_title("(b) the ONE calibrated term: row_depth(n_col)\nper-row ping-pong refill depth — "
                    "measured, not fit to a basis", fontsize=9.5)
    ax_rd.legend(fontsize=8, loc="lower right", framealpha=0.9)
    ax_rd.spines[["top", "right"]].set_visible(False)
    ax_rd.grid(color="#EEEEEE", zorder=0)

    fig.suptitle("Rowwise FIR — physical, near-fit-free cosim calibration  (T=8, 100 MHz)",
                 fontsize=12, y=1.02)
    _save_svg(fig, Path(out_svg))
    return Path(out_svg)


# ---------------------------------------------------------------------------
# Regenerate / check entry points
# ---------------------------------------------------------------------------

def regenerate() -> dict[str, Any]:
    """Render the committed SVG from the result JSONs and record provenance."""
    render(COMMITTED_SVG)
    status = {
        "figure": "sim_vs_cosim.svg",
        "sources": {"cosim_grid.json": _sha256(GRID_JSON),
                    "fir_calibration.json": _sha256(CALIB_JSON)},
        "svg_sha256": _sha256(COMMITTED_SVG),
        "regenerable_without_vitis": True,
    }
    SYNC_STATUS.write_text(json.dumps(status, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return status


def check() -> bool:
    """Render to a temp file and byte-compare against the committed SVG."""
    if not COMMITTED_SVG.exists():
        print(f"FAIL: committed SVG missing: {COMMITTED_SVG}")
        return False
    with tempfile.TemporaryDirectory() as d:
        fresh = render(Path(d) / "fresh.svg")
        ok = fresh.read_bytes() == COMMITTED_SVG.read_bytes()
    print("byte-identical" if ok else "MISMATCH vs committed SVG", f"({COMMITTED_SVG})")
    return ok


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Rowwise-FIR cosim-calibration figure (physical LT model).")
    ap.add_argument("--check", action="store_true",
                    help="render to a temp file and byte-compare against the committed SVG")
    args = ap.parse_args()
    if args.check:
        sys.exit(0 if check() else 1)
    st = regenerate()
    print(f"wrote {COMMITTED_SVG}  (svg sha256 {st['svg_sha256'][:12]})")
