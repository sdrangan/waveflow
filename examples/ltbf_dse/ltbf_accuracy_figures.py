"""ltbf_accuracy_figures.py -- the accuracy DSE's figures (plan step 3.3) from ``accuracy_losses.csv``.

Per target (``-6_14``, ``-6_3``), three figures in ``paper_data/figures/`` (local), each a row of panels,
one an array (8x8, 16x16, 32x32), one y-axis a panel (median loss in dB against the exact float LTBF
on the same signals; the 10th-90th percentile band where it helps):

* ``loss_vs_t_<target>.svg`` -- the factored form, ``r`` 1: one line a width (an ordinal blue ramp,
  narrow light to wide dark) and float CG (dashed ink), against the iterations ``t``;
* ``form_<target>.svg``      -- explicit against factored at ``t`` 3, ``r`` 1, against the width;
* ``rank_<target>.svg``      -- ``r`` 1 against 2 at ``t`` 3, factored, against the width.

Losses span four decades and float CG's can be slightly negative, so the y-axis is symmetric-log
(linear within +-0.01 dB).  The colours are the dataviz reference palette's pre-validated values
(ordinal ramp steps 250-650 on the light surface; categorical slots 1 and 2).  SVGs are written
without dates or random ids, so a re-run is byte for byte the same (AC7).

    python -m examples.ltbf_dse.ltbf_accuracy_figures
"""

from __future__ import annotations

import csv
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
CSV = HERE / "paper_data" / "accuracy_losses.csv"
OUT = HERE / "paper_data" / "figures"
ARRAYS = ("8x8", "16x16", "32x32")
TARGETS = ("-6_14", "-6_3")
WIDTHS = (10, 12, 14, 16, 20)
#: Ordinal blue ramp (narrow -> wide), categorical slots 1-2, ink and grid.
RAMP = ("#86b6ef", "#5598e7", "#2a78d6", "#1c5cab", "#104281")
FORM = {"factored": "#2a78d6", "explicit": "#eb6834"}
INK, MUTED, GRID, SURFACE = "#1f1f1e", "#6b6b68", "#e6e5e1", "#fcfcfb"


def load(path: Path = CSV) -> dict:
    """``{(array, target, form or 'float', W or None, t, r): row}``."""
    out = {}
    with path.open() as fh:
        for row in csv.DictReader(fh):
            cfg = row["config"]
            if cfg == "float":
                key = (row["array"], row["target"], "float", None)
            else:  # W<w>g<guard>l<lane><e|f>
                w = int(cfg[1 : cfg.index("g")])
                key = (
                    row["array"],
                    row["target"],
                    "explicit" if cfg.endswith("e") else "factored",
                    w,
                )
            out[(*key, int(row["t"]), int(row["r"]))] = {
                k: float(row[k])
                for k in ("median_loss_db", "p10_loss_db", "p90_loss_db")
            }
    return out


def _axes(fig, n):
    import matplotlib.pyplot as plt

    axs = fig.subplots(1, n, sharey=True)
    for ax in axs:
        ax.set_facecolor(SURFACE)
        ax.set_yscale("symlog", linthresh=0.01)
        ax.grid(True, color=GRID, linewidth=0.6)
        for s in ("top", "right"):
            ax.spines[s].set_visible(False)
        for s in ("left", "bottom"):
            ax.spines[s].set_color(MUTED)
        ax.tick_params(colors=MUTED, labelsize=8)
    del plt
    return axs


def _save(fig, name: str) -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / name
    fig.savefig(path, format="svg", metadata={"Date": None, "Creator": None},
                facecolor=SURFACE)  # fmt: skip
    return path


def loss_vs_t(data: dict, target: str) -> Path:
    import matplotlib.pyplot as plt

    fig = plt.figure(figsize=(10, 3.4), facecolor=SURFACE)
    axs = _axes(fig, len(ARRAYS))
    ts = sorted({k[4] for k in data if k[1] == target and k[2] == "float"})
    for ax, arr in zip(axs, ARRAYS, strict=True):
        for w, col in zip(WIDTHS, RAMP, strict=True):
            ys = [
                data[(arr, target, "factored", w, t, 1)]["median_loss_db"] for t in ts
            ]
            ax.plot(
                ts, ys, color=col, linewidth=2, marker="o", markersize=4, label=f"W {w}"
            )
        ys = [data[(arr, target, "float", None, t, 1)]["median_loss_db"] for t in ts]
        ax.plot(ts, ys, color=INK, linewidth=1.5, linestyle="--", label="float CG")
        ax.set_title(arr, color=INK, fontsize=10)
        ax.set_xlabel("CG iterations t", color=MUTED, fontsize=9)
    axs[0].set_ylabel("median loss vs exact LTBF (dB)", color=MUTED, fontsize=9)
    h, lab = axs[-1].get_legend_handles_labels()
    fig.legend(
        h, lab, fontsize=8, frameon=False, loc="center right"
    )  # beside, never on, the data
    fig.suptitle(f"Fixed-point CG loss, factored, r = 1, target [{target.replace('_', ', ')}] dB",
                 color=INK, fontsize=11)  # fmt: skip
    fig.tight_layout(rect=(0, 0, 0.9, 1))
    path = _save(fig, f"loss_vs_t_{target}.svg")
    plt.close(fig)
    return path


def _vs_width(data: dict, target: str, series: dict, name: str, title: str) -> Path:
    import matplotlib.pyplot as plt

    fig = plt.figure(figsize=(10, 3.4), facecolor=SURFACE)
    axs = _axes(fig, len(ARRAYS))
    for ax, arr in zip(axs, ARRAYS, strict=True):
        for label, (key, col, ls) in series.items():
            rows = [data[(arr, target, *key(w))] for w in WIDTHS]
            ax.fill_between(WIDTHS, [r["p10_loss_db"] for r in rows], [r["p90_loss_db"] for r in rows],
                            color=col, alpha=0.12, linewidth=0)  # fmt: skip
            ax.plot(WIDTHS, [r["median_loss_db"] for r in rows], color=col, linewidth=2,
                    linestyle=ls, marker="o", markersize=4, label=label)  # fmt: skip
        ax.set_title(arr, color=INK, fontsize=10)
        ax.set_xlabel("operand width W (bits)", color=MUTED, fontsize=9)
        ax.set_xticks(WIDTHS)
    axs[0].set_ylabel(
        "loss vs exact LTBF (dB): median, p10-p90", color=MUTED, fontsize=9
    )
    h, lab = axs[-1].get_legend_handles_labels()
    fig.legend(
        h, lab, fontsize=8, frameon=False, loc="center right"
    )  # beside, never on, the data
    fig.suptitle(
        f"{title}, target [{target.replace('_', ', ')}] dB", color=INK, fontsize=11
    )
    fig.tight_layout(rect=(0, 0, 0.9, 1))
    path = _save(fig, name)
    plt.close(fig)
    return path


def figures() -> list[Path]:
    import matplotlib

    matplotlib.use("Agg")
    matplotlib.rcParams["svg.hashsalt"] = "ltbf_dse"  # deterministic ids
    data = load()
    out = []
    for tg in TARGETS:
        out.append(loss_vs_t(data, tg))
        out.append(_vs_width(data, tg, {
            "factored": (lambda w: ("factored", w, 3, 1), FORM["factored"], "-"),
            "explicit": (lambda w: ("explicit", w, 3, 1), FORM["explicit"], "-"),
        }, f"form_{tg}.svg", "Explicit vs factored Q at t = 3, r = 1"))  # fmt: skip
        out.append(_vs_width(data, tg, {
            "r = 1": (lambda w: ("factored", w, 3, 1), FORM["factored"], "-"),
            "r = 2": (lambda w: ("factored", w, 3, 2), FORM["factored"], "--"),
        }, f"rank_{tg}.svg", "Projection rank at t = 3, factored"))  # fmt: skip
    return out


def findings(data: dict | None = None) -> dict:
    """The numbers the candidate findings quote: per (array, target) the narrowest width within 0.1
    dB median loss at t = 3 (factored, r 1), explicit minus factored at W 16, t 3, and the t beyond
    which W 16 stops improving by 0.01 dB."""
    data = data or load()
    out: dict = defaultdict(dict)
    for arr in ARRAYS:
        for tg in TARGETS:
            ok = [
                w
                for w in WIDTHS
                if data[(arr, tg, "factored", w, 3, 1)]["median_loss_db"] <= 0.1
            ]
            out[arr, tg]["min_W_0.1dB_t3"] = min(ok) if ok else None
            out[arr, tg]["explicit_minus_factored_W16_t3"] = round(
                data[(arr, tg, "explicit", 16, 3, 1)]["median_loss_db"]
                - data[(arr, tg, "factored", 16, 3, 1)]["median_loss_db"], 4)  # fmt: skip
            ts = sorted(
                {k[4] for k in data if k[0] == arr and k[1] == tg and k[2] == "float"}
            )
            ys = [data[(arr, tg, "factored", 16, t, 1)]["median_loss_db"] for t in ts]
            out[arr, tg]["W16_t_saturates"] = next(
                (t for t, a, b in zip(ts, ys, ys[1:], strict=False) if a - b < 0.01),
                ts[-1],
            )
    return dict(out)


if __name__ == "__main__":
    for p in figures():
        print(p)
    for k, v in findings().items():
        print(k, v)
