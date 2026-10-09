"""mimo_cg_figures.py — Phase 1's BER figures, rendered deterministically as SVG.

Step 1.4 of ``plans/mimo_cg/mimo_cg_paper_sims.md``.  Reads ``paper_data/float_ber.csv`` and
writes, to ``examples/mimo_cg/results/figures/``:

* ``float_ber_default.svg`` — the default configuration (M = 64, K = 8, 16-QAM);
* ``float_ber_<modulation>.svg`` — a 3 × 3 grid of small multiples (rows M, columns K).

Given ``paper_data/zf_crossings.csv``, each panel also marks the analytical ZF crossing, a small
× at (crossing, 1e-3).  The simulated ZF curve should pass through it, a visible check of the
simulator against theory.

Encoding: CG's iteration count is ordinal, so its curves share one blue ramp, light (few
iterations) to dark (``nit = K``).  Exact MMSE is solid primary ink and ZF dashed secondary ink.
The palette is the dataviz skill's validated reference instance, unchanged.

Determinism: a fixed ``svg.hashsalt``, text rendered as paths, and no date metadata, so a
forced re-run writes byte-identical files.
"""

from __future__ import annotations

import os
from collections import defaultdict
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from examples.mimo_cg.mimo_cg import (
    DEFAULT_CONFIG,
    K_VALUES,
    M_VALUES,
    TARGET_BER,
    read_table,
)
from examples.mimo_cg.mimo_link import MODULATIONS

INK = "#0b0b0b"
INK_SECONDARY = "#52514e"
GRID = "#e4e3df"
#: Blue ramp steps 250, 350, 450, 550, 650: ordinal, never lighter than 250 on a light surface.
CG_RAMP = ("#86b6ef", "#5598e7", "#2a78d6", "#1c5cab", "#104281")
#: A marker per CG series (1, 2, 3, 4, K iterations), so the ramp survives print and grayscale.
CG_MARKERS = ("o", "s", "^", "D", "v")
#: Markers at every 4th SNR point (every 4 dB), so they identify a curve without cluttering it.
MARK_EVERY = 4
MOD_LABEL = {"qpsk": "QPSK", "16qam": "16-QAM", "64qam": "64-QAM"}

STYLE = {
    "font.size": 8,
    "axes.titlesize": 8,
    "axes.edgecolor": INK_SECONDARY,
    "axes.labelcolor": INK,
    "axes.spines.top": False,
    "axes.spines.right": False,
    "xtick.color": INK_SECONDARY,
    "ytick.color": INK_SECONDARY,
    "axes.grid": True,
    "grid.color": GRID,
    "grid.linewidth": 0.5,
    "lines.linewidth": 1.5,
    "legend.frameon": False,
    "svg.hashsalt": "mimo_cg",
    "svg.fonttype": "path",
}


def _curves(rows: list[dict]) -> dict[tuple, dict[str, list[tuple[float, float]]]]:
    """``{(M, K, modulation): {detector: [(rho_db, ber), ...]}}``, with zero-BER points dropped."""
    out: dict[tuple, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    for r in rows:
        ber = float(r["ber"])
        if ber > 0:
            key = (int(r["M"]), int(r["K"]), r["modulation"])
            out[key][r["detector"]].append((float(r["rho_db"]), ber))
    return out


def _style_of(detector: str, K: int, k_label: bool) -> dict:
    """Line style of one detector.  Colour follows the entity: CG with 1, 2, 3, 4 iterations
    always takes ramp steps 0-3, and CG with ``K > 4`` iterations the darkest step 4, in every
    panel.  ``k_label`` names that series "K it." in small multiples, where K varies by panel.
    """
    if detector == "mmse":
        return {"color": INK, "linestyle": "-", "label": "exact MMSE", "zorder": 3}
    if detector == "zf":
        # Drawn above MMSE, dashed, so it stays visible where the two coincide.
        return {
            "color": INK_SECONDARY,
            "linestyle": (0, (3, 2)),
            "label": "ZF",
            "zorder": 4,
        }
    n = int(detector[2:])
    slot = n - 1 if n <= 4 else 4
    if n <= 4:
        label = f"CG, {n} it."
    else:
        label = "CG, K it." if k_label else f"CG, {n} it."
    return {
        "color": CG_RAMP[slot],
        "linestyle": "-",
        "marker": CG_MARKERS[slot],
        "markersize": 4.5,
        "markevery": MARK_EVERY,
        "label": label,
    }


def _panel(
    ax,
    curves: dict[str, list],
    title: str,
    K: int,
    zf_db: float | None = None,
    k_label: bool = False,
) -> None:
    nits = sorted(int(d[2:]) for d in curves if d.startswith("cg"))
    order = [f"cg{n}" for n in nits] + ["mmse", "zf"]
    for det in order:
        if det in curves:
            x, y = zip(*curves[det], strict=True)
            ax.semilogy(x, y, **_style_of(det, K, k_label))
    ax.axhline(TARGET_BER, color=INK_SECONDARY, linestyle=":", linewidth=0.8, zorder=1)
    if zf_db is not None:
        ax.plot(
            [zf_db],
            [TARGET_BER],
            marker="x",
            markersize=6,
            color=INK,
            zorder=4,
            label="ZF theory",
        )
    ax.set_ylim(1e-6, 0.6)
    ax.set_xlim(-20, 20)
    ax.set_title(title, color=INK)


def write_figures(
    float_ber_csv: os.PathLike,
    out_dir: os.PathLike,
    *,
    zf_crossings_csv: os.PathLike | None = None,
) -> list[Path]:
    """Render every Phase 1 figure from ``float_ber_csv`` into ``out_dir``; return the paths."""
    curves = _curves(read_table(float_ber_csv))
    zf = {}
    if zf_crossings_csv is not None:
        zf = {
            (int(r["M"]), int(r["K"]), r["modulation"]): float(r["zf_crossing_db"])
            for r in read_table(zf_crossings_csv)
        }
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    with plt.rc_context(STYLE):
        M, K, mod = DEFAULT_CONFIG
        fig, ax = plt.subplots(figsize=(4.2, 3.2))
        _panel(
            ax,
            curves[(M, K, mod)],
            f"M = {M}, K = {K}, {MOD_LABEL[mod]}",
            K,
            zf.get((M, K, mod)),
        )
        ax.set_xlabel("per-user transmit SNR ρ (dB)")
        ax.set_ylabel("uncoded BER")
        ax.legend(loc="lower left", fontsize=7)
        fig.tight_layout()
        written.append(_save(fig, out_dir / "float_ber_default.svg"))

        for mod in MODULATIONS:
            fig, axes = plt.subplots(
                len(M_VALUES),
                len(K_VALUES),
                figsize=(7.0, 6.2),
                sharex=True,
                sharey=True,
            )
            for i, M in enumerate(M_VALUES):
                for j, K in enumerate(K_VALUES):
                    _panel(
                        axes[i][j],
                        curves[(M, K, mod)],
                        f"M = {M}, K = {K}",
                        K,
                        zf.get((M, K, mod)),
                        k_label=True,
                    )
            for ax in axes[-1]:
                ax.set_xlabel("ρ (dB)")
            for ax in axes[:, 0]:
                ax.set_ylabel("uncoded BER")
            # The K = 16 panel carries every series (1, 2, 3, 4 and K iterations).
            handles, labels = axes[0][-1].get_legend_handles_labels()
            fig.suptitle(
                f"{MOD_LABEL[mod]}: CG-MMSE vs exact MMSE and ZF", y=0.99, color=INK
            )
            fig.legend(
                handles,
                labels,
                loc="upper center",
                bbox_to_anchor=(0.5, 0.955),
                ncol=4,
                fontsize=7,
            )
            fig.text(
                0.5,
                0.005,
                "In the K = 4 column the 4-iteration curve (◇) is also the K-iteration curve.",
                ha="center",
                va="bottom",
                fontsize=7,
                color=INK_SECONDARY,
            )
            fig.tight_layout(rect=(0, 0.025, 1, 0.885))
            written.append(_save(fig, out_dir / f"float_ber_{mod}.svg"))
    return written


def _save(fig, path: Path) -> Path:
    fig.savefig(path, format="svg", metadata={"Date": None})
    plt.close(fig)
    return path
