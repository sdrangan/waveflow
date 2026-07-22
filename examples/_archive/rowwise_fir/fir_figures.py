"""Committed-figure workflow for the rowwise-FIR streaming cosim-calibration (Stage B).

Renders ONE deterministic SVG telling the **occupancy-based, near-fit-free** calibration story:
the per-job throughput period is set by the bus **occupancy** (``read_words + write_words`` beats,
deterministic), with a single residual ``β`` (m_axi sustained cyc/beat).  The FIR serializes its
load and store on the shared bundle (so occupancy ADDS); the bundle itself is full-duplex
(``sandbox/duplex_toy``), so that is ~2× throughput headroom, not a bus limit.

The figure regenerates from **committed JSONs ALONE** — no Vitis, no cosim re-run, no VCD:

    examples/rowwise_fir/results/cosim_sweep.json       (measured period + latency + occupancy)
    examples/rowwise_fir/results/fir_calibration.json   (fitted params + Gate-B emergent sim-vs-cosim)

Two panels:
  (a) emergent sim (composed from the components) vs cosim parity for BOTH period and single-job
      latency across the size grid + the held-out point (circled) — all on y=x; the generalization.
  (b) period vs bus occupancy: the measured points + the line ``β·occupancy + bus_job`` — occupancy
      is the deterministic predictor (β the only residual).

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
matplotlib.rcParams["svg.hashsalt"] = "rowwise_fir_figures"
import matplotlib.pyplot as plt  # noqa: E402

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parents[1]
RESULTS = _HERE / "results"
SWEEP_JSON = RESULTS / "cosim_sweep.json"
CALIB_JSON = RESULTS / "fir_calibration.json"
COMMITTED_SVG = _REPO / "docs" / "examples" / "rowwise_fir" / "images" / "sim_vs_cosim.svg"
SYNC_STATUS = COMMITTED_SVG.with_name("sync_status.json")

_PER_C, _LAT_C, _HOLD_C = "#4C78A8", "#E45756", "#333333"


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _save_svg(fig, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, format="svg", bbox_inches="tight", metadata={"Date": None})
    plt.close(fig)


def _sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _occ(pt: dict) -> int:
    j0 = pt["per_job"][0]
    return j0["load_words"] + j0["store_words"]


def render(out_svg: Path) -> Path:
    sweep = _load(SWEEP_JSON)["points"]
    calib = _load(CALIB_JSON)
    val = calib["validation"]["per_point"]
    params = calib["model_params"]

    fig = plt.figure(figsize=(10.6, 4.6))
    gs = fig.add_gridspec(1, 2, width_ratios=[1.0, 1.05], wspace=0.28)
    ax_par = fig.add_subplot(gs[0, 0])
    ax_sc = fig.add_subplot(gs[0, 1])

    # -- Panel (a): sim vs cosim parity (period + latency) ------------------------
    def parity(series: str, color: str, label: str) -> None:
        fit = [(r[series]["cosim"], r[series]["sim"]) for r in val if not r["held_out"]]
        hold = [(r[series]["cosim"], r[series]["sim"]) for r in val if r["held_out"]]
        ax_par.scatter([c for c, _ in fit], [s for _, s in fit], s=42, color=color,
                       edgecolor="white", lw=0.6, zorder=3, label=label)
        ax_par.scatter([c for c, _ in hold], [s for _, s in hold], s=120, facecolors="none",
                       edgecolors=_HOLD_C, lw=1.8, zorder=4)

    allv = [r[s][k] for r in val for s in ("period", "latency") for k in ("sim", "cosim")]
    lim = max(allv) * 1.07
    ax_par.plot([0, lim], [0, lim], color="#BBBBBB", lw=1.2, zorder=1, label="y = x")
    parity("period", _PER_C, "period")
    parity("latency", _LAT_C, "single-job latency")
    ax_par.scatter([], [], s=80, facecolors="none", edgecolors=_HOLD_C, lw=1.8,
                   label=f"held out {calib['holdout']}")
    wp = calib["validation"]["worst_period_rel_err"] * 100
    wl = calib["validation"]["worst_latency_rel_err"] * 100
    ax_par.set_xlim(0, lim)
    ax_par.set_ylim(0, lim)
    ax_par.set_aspect("equal")
    ax_par.set_xlabel("measured — RTL cosim [cyc]")
    ax_par.set_ylabel("sim — fitted FIRTiming [cyc]")
    ax_par.set_title(f"(a) sim vs cosim: period & latency\nworst period {wp:.1f}%, "
                     f"latency {wl:.1f}% over the grid", fontsize=9.5)
    ax_par.legend(fontsize=8, loc="upper left", framealpha=0.9)
    ax_par.spines[["top", "right"]].set_visible(False)
    ax_par.grid(color="#EEEEEE", zorder=0)

    # -- Panel (b): period vs bus occupancy (the deterministic predictor) ---------
    for nr, marker in ((1, "o"), (4, "s")):
        pts = [p for p in sweep if p["n_row"] == nr]
        if not pts:
            continue
        ax_sc.scatter([_occ(p) for p in pts], [p["period_cyc"] for p in pts], s=44,
                      color=_PER_C, marker=marker, edgecolor="white", lw=0.5, zorder=3)
    ho = next(p for p in sweep if [p["n_row"], p["n_col"]] == calib["holdout"])
    ax_sc.scatter([_occ(ho)], [ho["period_cyc"]], s=130, facecolors="none", edgecolors=_HOLD_C,
                  lw=1.8, zorder=4)
    beta, bjob = params["bus_beat_cyc"], params["bus_job_cyc"]
    occ = np.array([_occ(p) for p in sweep], dtype=float)
    xx = np.linspace(0, occ.max() * 1.03, 100)
    ax_sc.plot(xx, beta * xx + bjob, color="#333333", lw=1.6, ls="--", zorder=2,
               label=f"period = {beta:.2f}·occupancy + {bjob:.0f}")
    ax_sc.scatter([], [], color=_PER_C, marker="o", label="cosim (n_row=1)")
    ax_sc.scatter([], [], color=_PER_C, marker="s", label="cosim (n_row=4)")
    ax_sc.scatter([], [], s=90, facecolors="none", edgecolors=_HOLD_C, lw=1.8,
                  label=f"held out {calib['holdout']}")
    ax_sc.set_xlabel("bus occupancy = read_words + write_words  (beats == nwords, exact)")
    ax_sc.set_ylabel("steady period [cyc]")
    ax_sc.set_title("(b) FIR serializes load+store: occupancy ADDS\n"
                    f"period ≈ β·occupancy, β = {beta:.2f} cyc/beat (bundle is full-duplex → headroom)",
                    fontsize=9.5)
    ax_sc.legend(fontsize=8, loc="upper left", framealpha=0.9)
    ax_sc.spines[["top", "right"]].set_visible(False)
    ax_sc.grid(color="#EEEEEE", zorder=0)

    fig.suptitle("Rowwise FIR — free-running streaming pipeline, occupancy-calibrated  (T=8, 100 MHz)",
                 fontsize=12, y=1.02)
    _save_svg(fig, Path(out_svg))
    return Path(out_svg)


def regenerate() -> dict[str, Any]:
    render(COMMITTED_SVG)
    status = {
        "figure": "sim_vs_cosim.svg",
        "sources": {"cosim_sweep.json": _sha256(SWEEP_JSON),
                    "fir_calibration.json": _sha256(CALIB_JSON)},
        "svg_sha256": _sha256(COMMITTED_SVG),
        "regenerable_without_vitis": True,
    }
    SYNC_STATUS.write_text(json.dumps(status, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return status


def check() -> bool:
    if not COMMITTED_SVG.exists():
        print(f"FAIL: committed SVG missing: {COMMITTED_SVG}")
        return False
    with tempfile.TemporaryDirectory() as d:
        fresh = render(Path(d) / "fresh.svg")
        ok = fresh.read_bytes() == COMMITTED_SVG.read_bytes()
    print("byte-identical" if ok else "MISMATCH vs committed SVG", f"({COMMITTED_SVG})")
    return ok


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Rowwise-FIR streaming cosim-calibration figure.")
    ap.add_argument("--check", action="store_true",
                    help="render to a temp file and byte-compare against the committed SVG")
    args = ap.parse_args()
    if args.check:
        sys.exit(0 if check() else 1)
    st = regenerate()
    print(f"wrote {COMMITTED_SVG}  (svg sha256 {st['svg_sha256'][:12]})")
