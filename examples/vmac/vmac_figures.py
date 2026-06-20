"""Committed-figure workflow for the VMAC-over-mm-queue timing example (Stages 4-5).

Renders ONE deterministic SVG telling the **II-decoupling calibration** story: the Stage-1
loosely-timed (LT) sim was *transaction-gated* and mispredicted timing two ways — it
underestimated absolute latency ~5x and predicted ``anorm`` faster than ``abcorr``.  Stage 5
calibrates the LT model against a Vitis RTL cosim **sweep** (``latency ~= depth + II*trips``,
fit on 3 sizes, validated on a held-out 4th) and re-times the sim by the pipeline schedule.

The figure regenerates from **committed, source-agnostic** JSONs ALONE — no Vitis, no cosim
re-run, no VCD:

    examples/vmac/timeline/sim_sweep.json   (source="sim": naive + calibrated + RTL per size)
    examples/vmac/timeline/sim_timeline.json (the calibrated 4x4 detail; ab_eq occupancy)

Three panels:
  (a) latency vs trips, across the sweep — naive-LT (underestimates) -> calibrated-LT (tracks)
      vs RTL truth; the held-out config is marked (the generalization test).
  (b) read-bus words anorm vs abcorr per size — calibrated occupancy is STILL transaction-driven
      (anorm = HALF), the bus-utilization result the LT model always got right.
  (c) calibrated latency anorm vs abcorr per size — now EQUAL (fixed-II), matching the RTL: the
      "same latency, freed bus" result the transaction-gated model could not represent.

Run::

    PYTHONPATH=. ../pysilicon-venv/Scripts/python.exe examples/vmac/vmac_figures.py          # regenerate
    PYTHONPATH=. ../pysilicon-venv/Scripts/python.exe examples/vmac/vmac_figures.py --check  # byte-match
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("svg")
# Deterministic SVG: a stable hashsalt fixes the element ids matplotlib would otherwise
# randomize, so a re-render only diffs when the figure truly changed (mirrors shared_mem_figures).
matplotlib.rcParams["svg.hashsalt"] = "vmac_mm_queue_figures"
import matplotlib.pyplot as plt  # noqa: E402

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parents[1]
TIMELINE_DIR = _HERE / "timeline"
SIM_SWEEP_JSON = TIMELINE_DIR / "sim_sweep.json"
COMMITTED_SVG = _REPO / "docs" / "examples" / "mmqueue" / "images" / "sim_vs_cosim.svg"
SYNC_STATUS = COMMITTED_SVG.with_name("sync_status.json")

_NAIVE_C, _CALIB_C, _RTL_C = "#54A24B", "#4C78A8", "#E45756"
_ANORM_C, _ABCORR_C = "#4C78A8", "#F58518"


def _load() -> dict:
    return json.loads(SIM_SWEEP_JSON.read_text(encoding="utf-8"))


def _save_svg(fig, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # metadata Date=None omits the <dc:date> matplotlib embeds by default (else non-deterministic).
    fig.savefig(path, format="svg", bbox_inches="tight", metadata={"Date": None})
    plt.close(fig)


def _sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# Rendering (reads only the committed sim_sweep.json)
# ---------------------------------------------------------------------------

def render(out_svg: Path) -> Path:
    sweep = _load()
    pts = sorted(sweep["points"], key=lambda p: p["trips"])
    cal = sweep["calibration"]
    trips = [p["trips"] for p in pts]
    labels = [f"{p['n_rows']}x{p['n_cols']}" for p in pts]
    naive = [p["naive_abcorr_ns"] for p in pts]
    calib = [p["calib_abcorr_ns"] for p in pts]
    rtl = [p["rtl_latency_ns"] for p in pts]
    hold_i = next((i for i, p in enumerate(pts) if p["held_out"]), None)

    fig = plt.figure(figsize=(10.0, 7.8))
    gs = fig.add_gridspec(2, 2, height_ratios=[1.18, 1.0], hspace=0.42, wspace=0.28)
    ax_lat = fig.add_subplot(gs[0, :])
    ax_words = fig.add_subplot(gs[1, 0])
    ax_eq = fig.add_subplot(gs[1, 1])

    # -- Panel (a): latency vs trips — naive -> calibrated -> RTL ------------------
    ax_lat.plot(trips, rtl, "-o", color=_RTL_C, lw=2.2, ms=8, zorder=4, label="RTL cosim (truth)")
    ax_lat.plot(trips, calib, "-s", color=_CALIB_C, lw=2.0, ms=7, zorder=3,
                label="calibrated LT (depth + II·trips)")
    ax_lat.plot(trips, naive, "-^", color=_NAIVE_C, lw=2.0, ms=7, zorder=3,
                label="naive LT (transaction-gated)")
    if hold_i is not None:
        ax_lat.scatter([trips[hold_i]], [rtl[hold_i]], s=320, facecolors="none",
                       edgecolors="#333", lw=1.8, zorder=5)
        ax_lat.annotate(
            f"held-out {labels[hold_i]}\nfit on the other 3\npred err {cal['holdout_rel_err'] * 100:.1f}%",
            xy=(trips[hold_i], rtl[hold_i]),
            xytext=(trips[hold_i] - 46, rtl[hold_i] * 0.62), fontsize=8.5, color="#333",
            ha="left", arrowprops=dict(arrowstyle="->", color="#333", lw=1.0))
    for x, lab in zip(trips, labels):
        ax_lat.annotate(lab, xy=(x, 0), xytext=(x, -max(rtl) * 0.055), fontsize=7.5,
                        color="#777", ha="center", annotation_clip=False)
    ax_lat.set_xlabel("trips = n_rows · ⌈n_cols / PF⌉   (PF=1)")
    ax_lat.set_ylabel("command latency [ns]")
    ax_lat.set_ylim(0, max(rtl) * 1.12)
    ax_lat.set_title(
        f"(a) calibrated LT tracks RTL across the sweep — fit depth={cal['depth']:.0f}, "
        f"II={cal['ii']:.1f} cyc/trip (R²={cal['r2']:.3f})\n"
        "naive LT underestimates ~5×; one II parameter, fit on 3 sizes, predicts the held-out 4th",
        fontsize=9.5)
    ax_lat.legend(fontsize=8.5, loc="upper left", framealpha=0.9)
    ax_lat.spines[["top", "right"]].set_visible(False)
    ax_lat.grid(color="#EEEEEE", zorder=0)

    # -- Panel (b): read-bus words anorm vs abcorr (still halved) ------------------
    x = range(len(pts))
    w = 0.38
    an_rw = [p["anorm_read_words"] for p in pts]
    ab_rw = [p["abcorr_read_words"] for p in pts]
    ax_words.bar([i - w / 2 for i in x], an_rw, w, label="anorm (ab_eq)", color=_ANORM_C,
                 edgecolor="white", zorder=3)
    ax_words.bar([i + w / 2 for i in x], ab_rw, w, label="abcorr", color=_ABCORR_C,
                 edgecolor="white", zorder=3)
    ax_words.set_xticks(list(x))
    ax_words.set_xticklabels(labels, fontsize=8)
    ax_words.set_ylabel("m_axi read-bus words")
    ax_words.set_ylim(0, max(ab_rw) * 1.2)
    ax_words.set_title("(b) bus still halved: anorm = ½ reads\n(occupancy stays transaction-driven)",
                       fontsize=9.5)
    ax_words.legend(fontsize=8, loc="upper left", framealpha=0.9)
    ax_words.spines[["top", "right"]].set_visible(False)
    ax_words.grid(axis="y", color="#DDDDDD", zorder=0)

    # -- Panel (c): calibrated latency anorm vs abcorr (now equal — fixed-II) ------
    an_lat = [p["calib_anorm_ns"] for p in pts]
    ab_lat = [p["calib_abcorr_ns"] for p in pts]
    ax_eq.bar([i - w / 2 for i in x], an_lat, w, label="anorm (ab_eq)", color=_ANORM_C,
              edgecolor="white", zorder=3)
    ax_eq.bar([i + w / 2 for i in x], ab_lat, w, label="abcorr", color=_ABCORR_C,
              edgecolor="white", zorder=3)
    ax_eq.set_xticks(list(x))
    ax_eq.set_xticklabels(labels, fontsize=8)
    ax_eq.set_ylabel("calibrated latency [ns]")
    ax_eq.set_ylim(0, max(ab_lat) * 1.18)
    ax_eq.set_title("(c) calibrated: anorm latency == abcorr\n(fixed-II — \"same latency, freed bus\")",
                    fontsize=9.5)
    ax_eq.legend(fontsize=8, loc="upper left", framealpha=0.9)
    ax_eq.spines[["top", "right"]].set_visible(False)
    ax_eq.grid(axis="y", color="#DDDDDD", zorder=0)

    fig.suptitle(
        "VMAC loosely-timed sim, cosim-calibrated (II-decoupling)  —  PF=1, 100 MHz\n"
        "off → calibrated → tracks RTL on held-out points; bus halved but latency fixed-II",
        fontsize=12, y=0.995)
    fig.text(0.01, 0.004,
             "Note: queue occupancy is sim-only (the RTL cosim has an m_axi but no command ring) "
             "— out of cosim scope.", fontsize=7.5, color="#777", ha="left")
    _save_svg(fig, Path(out_svg))
    return Path(out_svg)


# ---------------------------------------------------------------------------
# Regenerate / check entry points
# ---------------------------------------------------------------------------

def regenerate() -> dict[str, Any]:
    """Render the committed SVG from the sweep timeline and record provenance."""
    render(COMMITTED_SVG)
    status = {
        "figure": "sim_vs_cosim.svg",
        "sources": {"sim_sweep.json": _sha256(SIM_SWEEP_JSON)},
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
    ap = argparse.ArgumentParser(description="VMAC mm-queue Stage-5 figure (cosim-calibrated LT).")
    ap.add_argument("--check", action="store_true",
                    help="render to a temp file and byte-compare against the committed SVG")
    args = ap.parse_args()
    if args.check:
        sys.exit(0 if check() else 1)
    st = regenerate()
    print(f"wrote {COMMITTED_SVG}  (svg sha256 {st['svg_sha256'][:12]})")
