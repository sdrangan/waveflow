"""Committed-figure workflow for the VMAC-over-mm-queue timing example (Stage 4).

Renders ONE deterministic SVG that conveys the headline finding by overlaying the
loosely-timed sim against the Vitis RTL cosim.  The figure regenerates from the two
**committed, source-agnostic** timelines ALONE — no Vitis, no cosim re-run, no VCD:

    examples/vmac/timeline/sim_timeline.json    (source="sim",   timebase="ns")
    examples/vmac/timeline/cosim_timeline.json  (source="cosim", timebase="ns")

That is the whole "committed figure" property: the docs build never needs Vitis.

Three panels:
  (1) read-bus words per command  — anorm (ab_eq) issues HALF (16 vs 32); sim & RTL agree.
  (2) command latency, sim vs RTL — sim predicts anorm<abcorr (transaction-gated), RTL is
      FIXED-II (anorm==abcorr); and sim << RTL absolute (the underestimate).
  (3) per-command transaction Gantt (local time) — read (A/B) + write blocks: the sim's one
      whole-matrix LT block vs the RTL's per-word beats, and B's reads freed under ab_eq.

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
SIM_JSON = TIMELINE_DIR / "sim_timeline.json"
COSIM_JSON = TIMELINE_DIR / "cosim_timeline.json"
COMMITTED_SVG = _REPO / "docs" / "examples" / "mmqueue" / "images" / "sim_vs_cosim.svg"
SYNC_STATUS = COMMITTED_SVG.with_name("sync_status.json")

_COL = {"A": "#4C78A8", "B": "#F58518", "write": "#E45756"}
_SIM_C, _RTL_C = "#54A24B", "#E45756"


def _load() -> tuple[dict, dict]:
    sim = json.loads(SIM_JSON.read_text(encoding="utf-8"))
    cos = json.loads(COSIM_JSON.read_text(encoding="utf-8"))
    return sim, cos


def _cmd(d: dict, ab_eq: bool) -> dict:
    return next(c for c in d["commands"] if bool(c["ab_eq"]) == ab_eq)


def _cmd_t0(c: dict) -> float:
    """Command-local time origin: the first transaction's tstart."""
    return min((float(t["tstart"]) for t in c["transactions"]), default=0.0)


def _save_svg(fig, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # metadata Date=None omits the <dc:date> matplotlib embeds by default (else non-deterministic).
    fig.savefig(path, format="svg", bbox_inches="tight", metadata={"Date": None})
    plt.close(fig)


def _sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# Rendering (reads only the two committed timeline JSONs)
# ---------------------------------------------------------------------------

def render(out_svg: Path) -> Path:
    sim, cos = _load()
    sim_an, sim_ab = _cmd(sim, True), _cmd(sim, False)
    cos_an, cos_ab = _cmd(cos, True), _cmd(cos, False)

    fig = plt.figure(figsize=(10.0, 7.6))
    gs = fig.add_gridspec(2, 2, height_ratios=[1.0, 1.15], hspace=0.55, wspace=0.30)
    ax_words = fig.add_subplot(gs[0, 0])
    ax_lat = fig.add_subplot(gs[0, 1])
    ax_gantt = fig.add_subplot(gs[1, :])

    # -- Panel 1: read-bus words (sim & RTL agree; anorm = half of abcorr) --------
    labels = ["anorm\n(ab_eq)", "abcorr"]
    read_words = [sim_an["read_words"], sim_ab["read_words"]]  # identical in cosim
    bars = ax_words.bar(labels, read_words, color=[_COL["A"], _COL["B"]],
                        edgecolor="white", zorder=3, width=0.6)
    for b, w in zip(bars, read_words):
        ax_words.text(b.get_x() + b.get_width() / 2, w + 0.6, f"{w}w",
                      ha="center", va="bottom", fontsize=10, fontweight="bold")
    ax_words.set_ylabel("m_axi read-bus words")
    ax_words.set_ylim(0, max(read_words) * 1.25)
    ax_words.set_title("(a) read words: ab_eq issues HALF\n(sim & RTL agree: 16 vs 32)",
                       fontsize=10)
    ax_words.annotate("B read suppressed\n(B aliases A)", xy=(0, read_words[0]),
                      xytext=(0.05, max(read_words) * 0.92), fontsize=8.5, color="#444",
                      ha="left")
    ax_words.spines[["top", "right"]].set_visible(False)
    ax_words.grid(axis="y", color="#DDDDDD", zorder=0)

    # -- Panel 2: latency, sim vs RTL (fixed-II + absolute underestimate) ---------
    x = range(2)
    w = 0.36
    sim_lat = [sim_an["latency"], sim_ab["latency"]]
    rtl_lat = [cos_an["latency"], cos_ab["latency"]]
    bs = ax_lat.bar([i - w / 2 for i in x], sim_lat, w, label="sim (loosely-timed)",
                    color=_SIM_C, edgecolor="white", zorder=3)
    br = ax_lat.bar([i + w / 2 for i in x], rtl_lat, w, label="RTL cosim",
                    color=_RTL_C, edgecolor="white", zorder=3)
    for bars_ in (bs, br):
        for b in bars_:
            ax_lat.text(b.get_x() + b.get_width() / 2, b.get_height() + 40,
                        f"{b.get_height():.0f}", ha="center", va="bottom", fontsize=8.5)
    ax_lat.set_xticks(list(x))
    ax_lat.set_xticklabels(labels)
    ax_lat.set_ylabel("command latency [ns]")
    ax_lat.set_ylim(0, max(rtl_lat) * 1.22)
    ax_lat.set_title("(b) latency: sim predicts anorm<abcorr,\nRTL is FIXED-II (equal)",
                     fontsize=10)
    ax_lat.legend(fontsize=8, loc="upper left", framealpha=0.9)
    ax_lat.spines[["top", "right"]].set_visible(False)
    ax_lat.grid(axis="y", color="#DDDDDD", zorder=0)
    # call out the RTL fixed-II equality
    ax_lat.annotate("", xy=(0 + w / 2, rtl_lat[0]), xytext=(1 + w / 2, rtl_lat[1]),
                    arrowprops=dict(arrowstyle="<->", color="#777", lw=1.0))
    ax_lat.text(0.5, rtl_lat[0] * 0.5, "RTL: equal\n(fixed-II)", ha="center",
                fontsize=8, color="#555")

    # -- Panel 3: per-command transaction Gantt (command-local time) --------------
    lanes = [
        ("sim  anorm",  sim_an), ("sim  abcorr", sim_ab),
        ("RTL  anorm",  cos_an), ("RTL  abcorr", cos_ab),
    ]
    for row, (name, c) in enumerate(lanes):
        y = len(lanes) - 1 - row
        t0 = _cmd_t0(c)
        for t in c["transactions"]:
            color = _COL["write"] if t["rw"] == "write" else _COL.get(
                "A" if t["name"] == "A" else "B" if t["name"] == "B" else "x", "#9D9D9D")
            x0 = float(t["tstart"]) - t0
            width = max(float(t["tend"]) - float(t["tstart"]), 4.0)  # min visible width
            ax_gantt.barh(y, width, left=x0, height=0.62, color=color,
                          edgecolor="white", linewidth=0.4, zorder=3)
        ax_gantt.text(-60, y, name, ha="right", va="center", fontsize=9, family="monospace")
        ax_gantt.text(c["latency"] + 60, y, f"{c['latency']:.0f} ns",
                      ha="left", va="center", fontsize=8.5, color="#555")

    ax_gantt.set_yticks([])
    ax_gantt.set_ylim(-0.6, len(lanes) - 0.4)
    ax_gantt.set_xlim(-700, max(cos_an["latency"], cos_ab["latency"]) + 700)
    ax_gantt.set_xlabel("time within the command [ns]  (each command normalized to its own start)")
    ax_gantt.set_title(
        "(c) memory-transaction timeline: sim's one whole-matrix LT block vs the RTL's per-word "
        "beats; B's reads freed under ab_eq", fontsize=10)
    ax_gantt.spines[["top", "right", "left"]].set_visible(False)
    ax_gantt.grid(axis="x", color="#EEEEEE", zorder=0)
    legend_handles = [
        plt.Line2D([0], [0], color=_COL["A"], lw=8, label="A read"),
        plt.Line2D([0], [0], color=_COL["B"], lw=8, label="B read"),
        plt.Line2D([0], [0], color=_COL["write"], lw=8, label="Y write"),
    ]
    ax_gantt.legend(handles=legend_handles, fontsize=8, loc="lower right", ncol=3,
                    framealpha=0.9)

    fig.suptitle(
        "VMAC ab_eq: loosely-timed sim vs Vitis RTL cosim  (PF=1, 4x4, 100 MHz)\n"
        "same reads halved, but RTL latency is fixed-II (\"same latency, freed bus\")",
        fontsize=12, y=0.99)
    # queue occupancy is a sim-only quantity (the cosim kernel has no command ring).
    fig.text(0.01, 0.005,
             "Note: queue occupancy is sim-only (the RTL cosim has an m_axi but no command ring) "
             "— out of cosim scope.", fontsize=7.5, color="#777", ha="left")
    _save_svg(fig, Path(out_svg))
    return Path(out_svg)


# ---------------------------------------------------------------------------
# Regenerate / check entry points
# ---------------------------------------------------------------------------

def regenerate() -> dict[str, Any]:
    """Render the committed SVG from the two timelines and record provenance."""
    render(COMMITTED_SVG)
    status = {
        "figure": "sim_vs_cosim.svg",
        "sources": {
            "sim_timeline.json": _sha256(SIM_JSON),
            "cosim_timeline.json": _sha256(COSIM_JSON),
        },
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
    ap = argparse.ArgumentParser(description="VMAC mm-queue Stage-4 figure (sim vs cosim).")
    ap.add_argument("--check", action="store_true",
                    help="render to a temp file and byte-compare against the committed SVG")
    args = ap.parse_args()
    if args.check:
        sys.exit(0 if check() else 1)
    st = regenerate()
    print(f"wrote {COMMITTED_SVG}  (svg sha256 {st['svg_sha256'][:12]})")
