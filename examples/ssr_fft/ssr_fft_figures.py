"""ssr_fft_figures.py -- the docs figures: SsrFft against the vendor core, interval and resources.

    python -m examples.ssr_fft.ssr_fft_figures

``SsrFft``'s numbers come from ``measured.json`` (written by ``ssr_fft_measure``); ``VitisFft``'s from
the platform's committed calibration (``waveflow/calib/platforms/rfsoc4x2_bfm_250mhz``: the RTL frame
intervals its timing model was fit on, and its csynth resource records).  Nothing is transcribed.
"""
from __future__ import annotations

import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402

from waveflow.calib.fixtures.vitis_fft import VitisFftFixture  # noqa: E402
from waveflow.vitis_l1.testbench import default_platform_dir  # noqa: E402

HERE = Path(__file__).resolve().parent
OUT = HERE.parents[1] / "docs" / "examples" / "ssr_fft" / "images"

# The validated reference palette (categorical slots 1-2), light surface, text inks.
BLUE, ORANGE = "#2a78d6", "#eb6834"
SURFACE, INK, INK2, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3df"

#: Applied per figure (plt.rc_context), never to the process: an import-time rcParams.update
#: restyled -- and re-salted -- every figure rendered after it in the same process.
STYLE = {
    "figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "savefig.facecolor": SURFACE,
    "axes.edgecolor": GRID, "axes.labelcolor": INK2, "xtick.color": INK2, "ytick.color": INK2,
    "text.color": INK, "font.size": 10, "axes.grid": True, "grid.color": GRID,
    "grid.linewidth": 0.8, "axes.spines.top": False, "axes.spines.right": False,
    "svg.hashsalt": "ssr_fft", "svg.fonttype": "none",
}


def ssr_runs(reorder: str = "pingpong") -> dict[int, dict]:
    data = json.loads((HERE / "measured.json").read_text(encoding="utf-8"))
    return {r["L"]: r for r in data["runs"].values() if r["reorder"] == reorder}


def vitis_intervals(platform: Path) -> dict[int, float]:
    _, ii_m = VitisFftFixture.models(platform)
    out = {}
    for d in sorted((ii_m.calib_dir / "rtl").glob("L*")):
        out[int(d.name[1:])] = float(pd.read_csv(d / "firings.csv")["span"].astype(float).median())
    return dict(sorted(out.items()))


def vitis_resources(platform: Path) -> dict[int, dict]:
    out = {}
    for mod in sorted((platform / "modules").glob("vitis_fft-*")):
        ident = json.loads((mod / "module.json").read_text(encoding="utf-8"))
        if ident["params"].get("in_w") != 16 or ident["params"].get("tw_w") != 18:
            continue
        last = (mod / "resource" / "records.jsonl").read_text(encoding="utf-8").splitlines()[-1]
        out[int(ident["params"]["L"])] = json.loads(last)["payload"]
    return dict(sorted(out.items()))


def _xaxis(ax, xs) -> None:
    ax.set_xscale("log", base=2)
    ax.set_xticks(xs, [str(n) for n in xs])
    ax.minorticks_off()


@plt.rc_context(STYLE)
def interval(ssr: dict, vit: dict, path: Path) -> None:
    """Cycles a frame against L, log-log, both modules -- and L/R, which SsrFft sits on."""
    xs = [n for n in ssr if n in vit]
    fig, ax = plt.subplots(figsize=(6.4, 3.6))
    ax.plot(xs, [n / 4 for n in xs], color=INK2, lw=1, ls="--", zorder=2)
    ax.plot(xs, [vit[n] for n in xs], color=ORANGE, lw=2, marker="o", ms=7, zorder=3)
    ax.plot(xs, [ssr[n]["interval"][0] for n in xs], color=BLUE, lw=2, marker="s", ms=7, zorder=4)
    _xaxis(ax, xs)
    ax.set_yscale("log")
    ax.set_xlabel("FFT length L")
    ax.set_ylabel("cycles per frame, back to back")
    # Direct labels (two series and the reference): identity is never colour alone.
    n = xs[-2]
    ax.annotate("VitisFft (AMD's core, as shipped)", (n, vit[n]), xytext=(-8, 10),
                textcoords="offset points", ha="right", color=INK2)
    ax.annotate("SsrFft = L/R", (n, n / 4), xytext=(8, -16), textcoords="offset points",
                ha="left", color=INK2)
    for m in xs:
        ax.annotate(f"{vit[m] / ssr[m]['interval'][0]:.1f}×", (m, (vit[m] * m / 4) ** 0.5),
                    ha="center", va="center", color=INK2, fontsize=8.5)
    fig.tight_layout()
    fig.savefig(path, metadata={"Date": None})
    plt.close(fig)


@plt.rc_context(STYLE)
def resources(ssr: dict, vit: dict, path: Path) -> None:
    """Small multiples, one counter per panel (different units, so never one shared axis)."""
    xs = [n for n in ssr if n in vit]
    fig, axes = plt.subplots(1, 4, figsize=(9.6, 2.9))
    for ax, key, title in zip(axes, ("dsp", "bram", "lut", "ff"),
                              ("DSP", "BRAM (18K)", "LUT", "FF")):
        a = [vit[n][key] for n in xs]
        b = [ssr[n]["resources"][key] for n in xs]
        ax.plot(xs, a, color=ORANGE, lw=2, marker="o", ms=6, zorder=3)
        ax.plot(xs, b, color=BLUE, lw=2, marker="s", ms=6, zorder=4)
        _xaxis(ax, xs)
        ax.tick_params(axis="both", labelsize=8)
        ax.set_title(title, color=INK, fontsize=10, loc="left")
        ax.set_ylim(0, max(a + b) * 1.2 if max(a + b) else 1)
        ax.set_xlabel("L", fontsize=9)
    axes[0].legend(["VitisFft", "SsrFft"], frameon=False, fontsize=8, loc="upper left")
    fig.tight_layout()
    fig.savefig(path, metadata={"Date": None})
    plt.close(fig)


def main() -> None:
    platform = default_platform_dir()
    ssr = ssr_runs()
    OUT.mkdir(parents=True, exist_ok=True)
    interval(ssr, vitis_intervals(platform), OUT / "interval.svg")
    resources(ssr, vitis_resources(platform), OUT / "resources.svg")
    print(f"wrote {sorted(p.name for p in OUT.glob('*.svg'))} to {OUT}")


if __name__ == "__main__":
    main()
