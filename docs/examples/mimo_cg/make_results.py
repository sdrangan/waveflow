"""Result figures for docs/examples/mimo_cg/index.md, from the study's own outputs.

Run from the repo root:  python docs/examples/mimo_cg/make_results.py

Writes, to ``docs/examples/mimo_cg/images/``:

* ``result_guard_savings.svg`` — the narrowest register width W within 0.5 dB of float exact MMSE
  at BER 1e-3, per configuration, for guard g_s = 0, 4 and 8 (Phase 3,
  ``examples/mimo_cg/paper_data/accuracy_losses.csv``, recurrence residual, any nit);
* ``result_hw_resources.svg`` — csynth DSP / LUT / BRAM_18K of the vector unit, the matmul and the
  integrated detector at K = 4, 8, 16 (Phase 4, W12g8, default knobs);
* ``result_rtl_cycles.svg`` — the integrated detector at RTL (XSI): cycles between consecutive job
  completions against the job's CG iterations, and the divider fix.

The hardware numbers come from the Phase 4 build directories (``examples/mimo_cg/hw/build/``,
gitignored) when they exist, and are snapshotted to ``hw_csynth.csv`` and ``hw_rtl_cycles.csv``
next to this script, so the figures regenerate without Vitis.  Uses the Phase 1 plot style (deterministic SVG).
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT))

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from examples.mimo_cg.mimo_cg import read_table
from examples.mimo_cg.mimo_cg_accuracy_analysis import LOSS_BUDGET_DB
from examples.mimo_cg.mimo_cg_figures import (
    INK,
    INK_SECONDARY,
    MOD_LABEL,
    STYLE,
    _save,
)

IMAGES = HERE / "images"
CSYNTH_CSV = HERE / "hw_csynth.csv"
RTL_CSV = HERE / "hw_rtl_cycles.csv"
RESOURCES = ("DSP", "LUT", "FF", "BRAM_18K", "URAM")
BUILD = ROOT / "examples" / "mimo_cg" / "hw" / "build"
LOSSES = ROOT / "examples" / "mimo_cg" / "paper_data" / "accuracy_losses.csv"

#: Ordinal blue ramp (Phase 1 palette), light = no guard, dark = 8 guard bits.
GUARD_RAMP = {0: "#86b6ef", 4: "#2a78d6", 8: "#0d366b"}
#: Block colours, as in the concept diagrams.
VEC, MM, MM3, DET = "#4a3aa7", "#2a78d6", "#86b6ef", "#52514e"
NEUTRAL = "#c9c7c1"
#: The configurations, in the accuracy figures' order.
CONFIGS = [(M, K) for K in (4, 8, 16) for M in (32, 64, 128)]
#: The step 4.10 detector runs (W12g8 unless noted): K -> build directory.
DET_RUNS = {
    "K = 4": "cg_detector_k4_f0",
    "K = 4, W14g8": "cg_detector_k4_f1",
    "K = 8": "cg_detector_k8_f0",
    "K = 16": "cg_detector_k16_f0",
}
#: K = 4, 20 jobs, last done cycle before the divider fix (plan §15, M4 fix-up entry).
BEFORE_DIVIDER_FIX = {"W12g8": 161_254, "W14g8": 177_245}
#: The 4 ns target clock, for cycles -> µs.
CLOCK_NS = 4.0


# --- data ------------------------------------------------------------------------------------


def _csynth(build: Path) -> dict | None:
    from waveflow.utils.csynthparse import CsynthParser, synth_target

    rpts = list(build.glob("*_proj/solution1/syn/report"))
    if not rpts:
        return None
    p = CsynthParser(report_path=str(rpts[0]))
    p.get_total_resources()
    t = synth_target(rpts[0])
    return {
        **{k: int(v) for k, v in p.total_resources.items()},
        "est_ns": t["estimated_period_ns"],
        "available": {k: int(v) for k, v in p.available_resources.items()},
    }


def _write_snapshot(hw: dict) -> None:
    with open(CSYNTH_CSV, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(
            ["build", *RESOURCES, "est_ns", *(f"available_{k}" for k in RESOURCES)]
        )
        for name, r in hw["csynth"].items():
            w.writerow(
                [name, *(r[k] for k in RESOURCES), r["est_ns"]]
                + [r["available"][k] for k in RESOURCES]
            )
    with open(RTL_CSV, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["run", "job", "done_cycle"])
        for label, cycles in hw["rtl"].items():
            w.writerows([label, j, c] for j, c in enumerate(cycles))


def _read_snapshot() -> dict:
    hw: dict = {"csynth": {}, "rtl": {}}
    for r in read_table(CSYNTH_CSV):
        hw["csynth"][r["build"]] = {
            **{k: int(r[k]) for k in RESOURCES},
            "est_ns": float(r["est_ns"]),
            "available": {k: int(r[f"available_{k}"]) for k in RESOURCES},
        }
    for r in read_table(RTL_CSV):
        hw["rtl"].setdefault(r["run"], []).append(int(r["done_cycle"]))
    return hw


def collect() -> dict:
    """The hardware numbers from the build directories (and snapshot them), else the snapshot."""
    if not BUILD.is_dir():
        return _read_snapshot()
    hw: dict = {"csynth": {}, "rtl": {}}
    for d in sorted(BUILD.iterdir()):
        r = _csynth(d)
        if r is not None:
            hw["csynth"][d.name] = r
    for label, name in DET_RUNS.items():
        cyc = BUILD / name / "xsi" / "vectors" / "s_done" / "cycles.bin"
        if cyc.exists():
            hw["rtl"][label] = np.fromfile(cyc, dtype="<u8").tolist()
    _write_snapshot(hw)
    return hw


# --- figures ---------------------------------------------------------------------------------


def guard_savings(path: Path) -> Path:
    rows = [
        r
        for r in read_table(LOSSES)
        if r["detector"].startswith("fx:")
        and r["status"] == "ok"
        and r["residual"] == "recurrence"
        and float(r["loss_mmse_db"]) <= LOSS_BUDGET_DB
    ]
    narrowest: dict[tuple, int] = {}
    for r in rows:
        key = (r["modulation"], int(r["M"]), int(r["K"]), int(r["g_s"]))
        narrowest[key] = min(narrowest.get(key, 99), int(r["W"]))
    fig, axes = plt.subplots(1, 3, figsize=(7.0, 3.1), sharey=True)
    x = np.arange(len(CONFIGS))
    bw = 0.27
    for ax, mod in zip(axes, ("qpsk", "16qam", "64qam"), strict=True):
        for i, g in enumerate((0, 4, 8)):
            ws = [narrowest.get((mod, M, K, g)) for M, K in CONFIGS]
            xs = x + (i - 1) * bw
            ok = [w is not None for w in ws]
            ax.bar(
                xs[ok],
                [w for w in ws if w is not None],
                bw,
                color=GUARD_RAMP[g],
                label=f"g_s = {g}",
                zorder=2,
            )
            for xi, w in zip(xs, ws, strict=True):
                if w is None:  # no width in the sweep meets the budget
                    ax.bar(
                        xi, 21, bw, color="none", edgecolor=NEUTRAL, hatch="////", lw=0
                    )
                    ax.text(
                        xi,
                        21.2,
                        "none\n≤ 20",
                        ha="center",
                        va="bottom",
                        fontsize=6,
                        color=INK_SECONDARY,
                    )
        ax.set_title(MOD_LABEL[mod])
        ax.set_xticks(x, [f"{M}×{K}" for M, K in CONFIGS], rotation=60, fontsize=6.5)
        ax.set_ylim(6, 23)
        ax.set_yticks(range(6, 22, 2))
        ax.grid(axis="x", visible=False)
    axes[0].set_ylabel("narrowest W (bits)")
    for ax in axes:
        ax.set_xlabel("M × K", fontsize=7)
    handles, labels = axes[0].get_legend_handles_labels()
    fig.legend(
        handles,
        labels,
        loc="upper center",
        ncol=3,
        bbox_to_anchor=(0.5, 0.9),
        fontsize=7,
    )
    fig.suptitle(
        "Narrowest register width within 0.5 dB of float exact MMSE at BER 1e-3\n"
        "8 guard bits on pᴴAp and rᴴr only save 2–6 bits (median 4) on every register",
        fontsize=8,
    )
    fig.tight_layout(rect=(0, 0, 1, 0.84))
    return _save(fig, path)


def hw_resources(hw: dict, path: Path) -> Path:
    cs = hw["csynth"]
    Ks = (4, 8, 16)
    series = [
        ("vector unit (L = 4)", VEC, [cs.get(f"cg_vec_unit_k{K}_f0") for K in Ks]),
        ("matmul, 4-mult", MM, [cs.get(f"cg_mm_unit_k{K}_f0_cmul4") for K in Ks]),
        (
            "matmul, 3-mult (K = 4, 8)",
            MM3,
            [cs.get(f"cg_mm_unit_k{K}_f0_cmul3") for K in Ks],
        ),
        ("integrated detector", DET, [cs.get(f"cg_detector_k{K}_f0") for K in Ks]),
    ]
    metrics = (
        ("DSP", "DSP", 1),
        ("LUT", "LUT (thousands)", 1e-3),
        ("BRAM_18K", "BRAM_18K", 1),
    )
    avail = cs["cg_detector_k4_f0"]["available"]
    fig, axes = plt.subplots(1, 3, figsize=(7.0, 2.9))
    x = np.arange(len(Ks))
    bw = 0.2
    for ax, (key, label, scale) in zip(axes, metrics, strict=True):
        for i, (name, color, builds) in enumerate(series):
            for xi, b in zip(x + (i - 1.5) * bw, builds, strict=True):
                if b is None:
                    continue
                ax.bar(
                    xi,
                    b[key] * scale,
                    bw,
                    color=color,
                    zorder=2,
                    label=name if xi == x[0] + (i - 1.5) * bw else None,
                )
                if name == "integrated detector":
                    ax.text(
                        xi,
                        b[key] * scale,
                        f"{100 * b[key] / avail[key]:.1f}%",
                        ha="center",
                        va="bottom",
                        fontsize=6,
                        color=INK,
                    )
        ax.set_title(label)
        ax.set_xticks(x, [f"K = {K}" for K in Ks])
        ax.grid(axis="x", visible=False)
        ax.margins(y=0.12)
    axes[0].legend(loc="upper left", fontsize=6.5)
    est = [b["est_ns"] for b in cs.values()]
    fig.suptitle(
        f"csynth on xczu48dr at 4 ns, W = 12 with 8 guard bits: estimated clock "
        f"{min(est):.2f}–{max(est):.2f} ns for every build\n"
        "% = the integrated detector's share of the device",
        fontsize=8,
    )
    fig.tight_layout()
    return _save(fig, path)


def _job_gaps(cycles: list[int], K: int) -> tuple[np.ndarray, np.ndarray]:
    """(nit, cycles since the previous done) for every job after the first.

    The step 4.10 runs issue each problem at nit = 1..K in turn (``_every_nit`` in
    tests/examples/test_mimo_cg_hw_detector.py), so job j runs nit = j % K + 1.
    """
    c = np.asarray(cycles, dtype=float)
    nit = np.arange(len(c)) % K + 1
    return nit[1:], np.diff(c)


def rtl_cycles(hw: dict, path: Path) -> Path:
    fig, (ax, bx) = plt.subplots(
        1, 2, figsize=(7.0, 2.8), gridspec_kw={"width_ratios": [2.2, 1]}
    )
    styles = {
        "K = 4": (MM3, "o", "-"),
        "K = 4, W14g8": (MM3, "s", ":"),
        "K = 8": (MM, "^", "-"),
        "K = 16": ("#0d366b", "D", "-"),
    }
    for label, cycles in hw["rtl"].items():
        K = int(label.split(",")[0].split("=")[1])
        nit, gap = _job_gaps(cycles, K)
        slope, icept = np.polyfit(nit, gap, 1)
        color, marker, ls = styles[label]
        ax.plot(nit, gap / 1e3, marker, color=color, ms=4, zorder=3)
        xs = np.array([1, K])
        ax.plot(
            xs,
            (icept + slope * xs) / 1e3,
            ls,
            color=color,
            lw=1.2,
            label=f"{label}: {icept:,.0f} + {slope:,.0f}·nit",
        )
    ax.set_xlabel("CG iterations of the job (nit)")
    ax.set_ylabel("cycles per job (thousands)")
    ax.set_xticks([1, 2, 4, 8, 12, 16])
    ax.legend(
        loc="upper left",
        fontsize=6.5,
        title="measured, back-to-back jobs",
        title_fontsize=6.5,
    )
    sec = ax.secondary_yaxis(
        "right", functions=(lambda k: k * CLOCK_NS, lambda us: us / CLOCK_NS)
    )
    sec.set_ylabel("µs at 250 MHz", color=INK_SECONDARY)
    ax.set_title("Integrated detector at RTL (XSI): a job of N = 32 vectors")

    after = {"W12g8": hw["rtl"]["K = 4"][-1], "W14g8": hw["rtl"]["K = 4, W14g8"][-1]}
    x = np.arange(2)
    bw = 0.36
    for i, (name, vals, color) in enumerate(
        (
            ("guarded divide", BEFORE_DIVIDER_FIX, NEUTRAL),
            ("safe divisor + select", after, MM),
        )
    ):
        v = [vals["W12g8"] / 1e3, vals["W14g8"] / 1e3]
        bars = bx.bar(x + (i - 0.5) * bw, v, bw, color=color, label=name, zorder=2)
        for b, val in zip(bars, v, strict=True):
            bx.text(
                b.get_x() + bw / 2,
                val,
                f"{val:.0f}k",
                ha="center",
                va="bottom",
                fontsize=6.5,
            )
    speed = BEFORE_DIVIDER_FIX["W12g8"] / after["W12g8"]
    bx.set_xticks(x, ["W12g8", "W14g8"])
    bx.set_ylabel("cycles, 20 jobs (thousands)")
    bx.set_title(f"Divider fix, K = 4: {speed:.1f}× faster")
    bx.legend(loc="upper right", fontsize=6.5)
    bx.set_ylim(0, 1.45 * BEFORE_DIVIDER_FIX["W14g8"] / 1e3)
    bx.grid(axis="x", visible=False)
    fig.tight_layout()
    return _save(fig, path)


def main() -> None:
    hw = collect()
    with plt.rc_context(STYLE):
        for p in (
            guard_savings(IMAGES / "result_guard_savings.svg"),
            hw_resources(hw, IMAGES / "result_hw_resources.svg"),
            rtl_cycles(hw, IMAGES / "result_rtl_cycles.svg"),
        ):
            print("wrote", p)


if __name__ == "__main__":
    main()
