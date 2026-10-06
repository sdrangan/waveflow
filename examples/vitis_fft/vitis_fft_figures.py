"""vitis_fft_figures.py — the docs figures, rendered from the platform's committed calibration.

    python -m examples.vitis_fft.vitis_fft_figures

Reads ``waveflow/calib/platforms/rfsoc4x2_bfm_250mhz`` (the RTL firings the timing models were fit on,
and the csynth resource records) and writes SVGs into ``docs/examples/vitis_fft/images/``.  Nothing is
transcribed: recalibrate the platform, rerun this, and the figures follow.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402

from waveflow.calib.fixtures.vitis_fft import LENGTHS, VitisFftFixture  # noqa: E402
from waveflow.vitis_l1.testbench import default_platform_dir  # noqa: E402

HERE = Path(__file__).resolve().parent
OUT = HERE.parents[1] / "docs" / "examples" / "vitis_fft" / "images"

# The validated reference palette (categorical slots 1-2), light surface, text inks.
BLUE, ORANGE = "#2a78d6", "#eb6834"
SURFACE, INK, INK2, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3df"

plt.rcParams.update({
    "figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "savefig.facecolor": SURFACE,
    "axes.edgecolor": GRID, "axes.labelcolor": INK2, "xtick.color": INK2, "ytick.color": INK2,
    "text.color": INK, "font.size": 10, "axes.grid": True, "grid.color": GRID,
    "grid.linewidth": 0.8, "axes.spines.top": False, "axes.spines.right": False,
    "svg.hashsalt": "vitis_fft", "svg.fonttype": "none",
})


def _spans(model_dir: Path, length: int) -> pd.Series:
    return pd.read_csv(model_dir / "rtl" / f"L{length}" / "firings.csv")["span"].astype(float)


def timing_per_sample(proc_dir: Path, ii_dir: Path, path: Path) -> None:
    """Cycles per L/R word, against L: the frame interval and the isolated-frame processing span
    (mean, with its min-max range across arrival phases).  One axis -- both are cycles per word."""
    xs = list(LENGTHS)
    pl = [n / 4 for n in xs]
    ii = [_spans(ii_dir, n).median() / p for n, p in zip(xs, pl)]
    pm = [_spans(proc_dir, n).mean() / p for n, p in zip(xs, pl)]
    lo = [m - _spans(proc_dir, n).min() / p for n, p, m in zip(xs, pl, pm)]
    hi = [_spans(proc_dir, n).max() / p - m for n, p, m in zip(xs, pl, pm)]

    fig, ax = plt.subplots(figsize=(6.4, 3.4))
    ax.plot(xs, ii, color=ORANGE, lw=2, marker="o", ms=7, zorder=3)
    ax.errorbar(xs, pm, yerr=[lo, hi], color=BLUE, lw=2, marker="s", ms=7, capsize=4,
                elinewidth=1.5, zorder=4)
    ax.set_xscale("log", base=2)
    ax.set_xticks(xs, [str(n) for n in xs])
    ax.set_xlabel("FFT length L")
    ax.set_ylabel("cycles per L/R words")
    ax.set_ylim(0, max(ii + [m + h for m, h in zip(pm, hi)]) * 1.18)
    ax.minorticks_off()
    # Direct labels (two series): identity is never colour alone.
    ax.annotate("frame interval (back to back)", (xs[-1], ii[-1]), xytext=(-6, 10),
                textcoords="offset points", ha="right", color=INK2)
    ax.annotate("processing span, isolated frame\n(mean, min–max over arrival phase)",
                (xs[2], pm[2] - lo[2]), xytext=(0, -30), textcoords="offset points",
                ha="center", color=INK2)
    ax.axvspan(256 * 1.35, 1024 / 1.35, color=GRID, alpha=0.6, lw=0, zorder=0)
    ax.text(512, ax.get_ylim()[1] * 0.04, "implementation\nchange", ha="center", va="bottom",
            color=INK2, fontsize=8.5)
    fig.tight_layout()
    fig.savefig(path, metadata={"Date": None})
    plt.close(fig)


def phase_spread(proc_dir: Path, path: Path) -> None:
    """Every isolated frame's processing span, as a percentage off the mean at its length: the
    error the LT model's single number makes, frame by frame."""
    xs = [n for n in LENGTHS if n > 16]
    fig, ax = plt.subplots(figsize=(6.4, 2.9))
    for i, n in enumerate(xs):
        s = _spans(proc_dir, n)
        dev = 100 * (s - s.mean()) / s.mean()
        jitter = [(k % 7 - 3) * 0.045 for k in range(len(dev))]
        ax.scatter(dev, [i + j for j in jitter], s=22, color=BLUE, alpha=0.75, linewidths=0,
                   zorder=3)
        ax.text(dev.max() + 1.2, i, f"{dev.min():+.0f}% … {dev.max():+.0f}%", va="center",
                color=INK2, fontsize=9)
    ax.axvline(0, color=INK2, lw=1, zorder=2)
    ax.set_yticks(range(len(xs)), [f"L = {n}" for n in xs])
    ax.set_xlabel("processing span vs. the model's mean (%)")
    ax.set_xlim(-20, 16)
    ax.grid(axis="y", visible=False)
    fig.tight_layout()
    fig.savefig(path, metadata={"Date": None})
    plt.close(fig)


def _resources(platform: Path) -> dict[int, dict]:
    out = {}
    for mod in sorted((platform / "modules").glob("vitis_fft-*")):
        ident = json.loads((mod / "module.json").read_text(encoding="utf-8"))
        if ident["params"].get("in_w") != 16 or ident["params"].get("tw_w") != 18:
            continue
        last = (mod / "resource" / "records.jsonl").read_text(encoding="utf-8").splitlines()[-1]
        out[int(ident["params"]["L"])] = json.loads(last)["payload"]
    return dict(sorted(out.items()))


def resources(platform: Path, path: Path) -> None:
    """Small multiples, one counter per panel (different units, so never one shared axis)."""
    res = _resources(platform)
    xs = list(res)
    fig, axes = plt.subplots(1, 4, figsize=(9.6, 2.7))
    for ax, key, title in zip(axes, ("dsp", "bram", "lut", "ff"),
                              ("DSP", "BRAM (18K)", "LUT", "FF")):
        ys = [res[n][key] for n in xs]
        ax.plot(xs, ys, color=BLUE, lw=2, marker="o", ms=6, zorder=3)
        ax.set_xscale("log", base=2)
        ax.set_xticks(xs, [str(n) for n in xs], fontsize=8)
        ax.minorticks_off()
        ax.set_title(title, color=INK, fontsize=10, loc="left")
        ax.set_ylim(0, max(ys) * 1.2 if max(ys) else 1)
        ax.tick_params(axis="y", labelsize=8)
        ax.set_xlabel("L", fontsize=9)
    s = [round(math.log(n, 4)) for n in xs]
    axes[0].plot(xs, [12 * (k - 1) for k in s], color=INK2, lw=1, ls="--", zorder=2)
    axes[0].text(xs[1], 12 * (s[-1] - 1) * 0.92, "12·(stages − 1)", color=INK2, fontsize=8)
    fig.tight_layout()
    fig.savefig(path, metadata={"Date": None})
    plt.close(fig)


def main() -> None:
    platform = default_platform_dir()
    proc_m, ii_m = VitisFftFixture.models(platform)
    OUT.mkdir(parents=True, exist_ok=True)
    timing_per_sample(proc_m.calib_dir, ii_m.calib_dir, OUT / "timing_per_sample.svg")
    phase_spread(proc_m.calib_dir, OUT / "phase_spread.svg")
    resources(platform, OUT / "resources.svg")
    print(f"wrote {sorted(p.name for p in OUT.glob('*.svg'))} to {OUT}")


if __name__ == "__main__":
    main()
