"""mimo_cg_accuracy_figures.py — Phase 3's accuracy figures, rendered deterministically as SVG.

Step 3.4 of ``plans/mimo_cg/mimo_cg_paper_sims.md``.  Reads ``paper_data/accuracy_grid.csv`` and
``paper_data/accuracy_losses.csv`` and writes, to ``docs/examples/mimo_cg/images/``:

* ``accuracy_ber_<M>x<K>_<modulation>.svg`` — BER families of the fixed-point CG at
  ``nit = K``, one panel per guard g_s, against float CG and float exact MMSE on the same
  samples;
* ``accuracy_loss_<M>x<K>_<modulation>.svg`` — the SNR loss against float exact MMSE at BER
  1e-3 of every (W, nit), one heatmap per g_s, with ``floor`` cells;
* ``accuracy_min_width.svg`` — per configuration and ``nit``, the narrowest design (W, then g_s)
  within 0.5 dB of float exact MMSE (top row) and of float CG at the same ``nit`` (bottom row,
  the quantization-only budget).

Encoding (the dataviz skill's reference palette, validated with its ``validate_palette.py``):
W is ordinal, so the BER families take one blue ramp, light (narrow) to dark (wide).  Seven
lines cannot keep the ramp's minimum lightness gap, so the families show W ∈ {8, 12, 16, 20}
(steps 250, 400, 550, 700), and the heatmaps carry every W.  Loss and width are magnitudes on
the sequential blue ramp, steps 100–700, with the value printed in every cell, so colour is
never the only carrier.  A cell outside the encoding (a floor, or no design in the budget) is
the neutral grey.

Determinism: the Phase 1 style (fixed ``svg.hashsalt``, text as paths, no date metadata), so a
forced re-run writes byte-identical files.
"""

from __future__ import annotations

import os
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.patheffects as pe
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.colors import BoundaryNorm, ListedColormap

from examples.mimo_cg.mimo_cg import (
    DEFAULT_CONFIG,
    K_VALUES,
    M_VALUES,
    TARGET_BER,
    read_table,
)
from examples.mimo_cg.mimo_cg_accuracy_analysis import (
    FLOOR,
    LOSS_BUDGET_DB,
    design_curves,
)
from examples.mimo_cg.mimo_cg_figures import INK, INK_SECONDARY, MOD_LABEL, STYLE, _save
from examples.mimo_cg.mimo_link import MODULATIONS

#: The BER-family widths and their ordinal ramp steps (250, 400, 550, 700).
FAMILY_W = (8, 12, 16, 20)
FAMILY_RAMP = ("#86b6ef", "#3987e5", "#1c5cab", "#0d366b")
FAMILY_MARKERS = ("o", "s", "^", "D")
#: The sequential ramp, steps 100–700 in steps of 100; ink text on the first four, white after.
HEAT = ("#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95", "#0d366b")
HEAT_DARK_FROM = 4
NEUTRAL = "#f0efec"
#: The chart surface, which blanks the cells where nit > K (no such design exists).
SURFACE = "#fcfcfb"
#: Reference curves drawn over the BER families: thin, with a 2-pt surface halo.
REFERENCE = {
    "linewidth": 1.0,
    "path_effects": [pe.Stroke(linewidth=3.0, foreground=SURFACE), pe.Normal()],
}
WHITE = "#ffffff"
#: Loss bins (dB): the three lightest steps are within the 0.5 dB budget.
LOSS_BOUNDS = (0.1, 0.25, LOSS_BUDGET_DB, 1.0, 2.0, 4.0)
#: Configurations whose BER families and loss heatmaps are rendered: the default, and the
#: hardest (M/K = 2, 64-QAM).
DETAIL_CASES = ((*DEFAULT_CONFIG, "recurrence"), (32, 16, "64qam", "recurrence"))


def _case_key(M: int, K: int, modulation: str, residual: str) -> tuple:
    return (modulation, M, K, residual)


def _nonzero(points: list[tuple[float, float]]) -> tuple[list, list]:
    pts = [(s, b) for s, b in points if b > 0]
    return [s for s, _ in pts], [b for _, b in pts]


def _ber_family(curves: dict, case: tuple, guards: list[int], path: Path) -> Path:
    modulation, M, K, _ = case
    dets = curves[case]
    fig, axes = plt.subplots(1, len(guards), figsize=(7.0, 2.9), sharey=True)
    for ax, g in zip(axes, guards, strict=True):
        # The references sit on top, thin, with a surface halo, so they stay visible where a wide
        # format coincides with them (the families are plotted under them).
        x, y = _nonzero(dets["mmse"]["points"])
        ax.semilogy(x, y, color=INK, label="float exact MMSE", **REFERENCE, zorder=5)
        for slot, W in enumerate(FAMILY_W):
            x, y = _nonzero(dets[f"fx:W{W}g{g}:cg{K}"]["points"])
            ax.semilogy(
                x,
                y,
                color=FAMILY_RAMP[slot],
                marker=FAMILY_MARKERS[slot],
                markersize=4.5,
                label=f"W = {W}",
                zorder=3,
            )
        x, y = _nonzero(dets[f"cg{K}"]["points"])
        ax.semilogy(
            x,
            y,
            color=INK_SECONDARY,
            linestyle=(0, (3, 2)),
            label=f"float CG, {K} it.",
            **REFERENCE,
            zorder=6,
        )
        ax.axhline(
            TARGET_BER, color=INK_SECONDARY, linestyle=":", linewidth=0.8, zorder=1
        )
        ax.set_title(f"guard g_s = {g}", color=INK)
        ax.set_xlabel("ρ (dB)")
    axes[0].set_ylabel("uncoded BER")
    handles, labels = axes[0].get_legend_handles_labels()
    fig.suptitle(
        f"M = {M}, K = {K}, {MOD_LABEL[modulation]}: fixed-point CG at {K} iterations",
        y=0.99,
        color=INK,
    )
    fig.legend(
        handles,
        labels,
        loc="upper center",
        bbox_to_anchor=(0.5, 0.93),
        ncol=6,
        fontsize=7,
    )
    fig.tight_layout(rect=(0, 0, 1, 0.87))
    return _save(fig, path)


def _cell_text(
    ax, i: int, j: int, text: str, slot: int | None, bold: bool = False
) -> None:
    dark = slot is not None and slot >= HEAT_DARK_FROM
    ax.text(
        j,
        i,
        text,
        ha="center",
        va="center",
        fontsize=6,
        color=WHITE if dark else INK,
        fontweight="bold" if bold else "normal",
    )


def _loss_label(v: float) -> str:
    return f"{round(v, 1) + 0.0:.1f}" if abs(v) < 10 else f"{v:.0f}"


def _loss_heatmap(
    losses: list[dict], case: tuple, guards: list[int], path: Path
) -> Path:
    modulation, M, K, residual = case
    rows = [
        r
        for r in losses
        if (r["modulation"], int(r["M"]), int(r["K"]), r["residual"]) == case
        and r["detector"].startswith("fx:")
    ]
    widths = sorted({int(r["W"]) for r in rows})
    nits = sorted({int(r["nit"]) for r in rows})
    cmap = ListedColormap(HEAT).with_extremes(bad=NEUTRAL)
    norm = BoundaryNorm(LOSS_BOUNDS, len(HEAT), extend="both")
    fig, axes = plt.subplots(
        1, len(guards), figsize=(7.0, 3.2), sharey=True, layout="constrained"
    )
    for ax, g in zip(axes, guards, strict=True):
        V = np.full((len(widths), len(nits)), np.nan)
        for r in rows:
            if int(r["g_s"]) == g and r["status"] != FLOOR:
                V[widths.index(int(r["W"])), nits.index(int(r["nit"]))] = float(
                    r["loss_mmse_db"]
                )
        im = ax.imshow(
            np.ma.masked_invalid(V), cmap=cmap, norm=norm, origin="lower", aspect="auto"
        )
        for i in range(len(widths)):
            for j in range(len(nits)):
                v = V[i, j]
                if np.isnan(v):
                    _cell_text(ax, i, j, "∞", None)  # a floor: the loss is unbounded
                else:
                    slot = int(np.searchsorted(LOSS_BOUNDS, v, side="right"))
                    _cell_text(ax, i, j, _loss_label(v), slot, bold=v <= LOSS_BUDGET_DB)
        ax.set_xticks(range(len(nits)), [str(n) for n in nits])
        ax.set_yticks(range(len(widths)), [str(w) for w in widths])
        ax.set_xlabel("CG iterations")
        ax.set_title(f"guard g_s = {g}", color=INK)
        ax.grid(False)
    axes[0].set_ylabel("register width W (bits)")
    cbar = fig.colorbar(im, ax=list(axes), spacing="uniform", fraction=0.04, pad=0.02)
    cbar.set_label("SNR loss vs float exact MMSE (dB)")
    cbar.outline.set_edgecolor(INK_SECONDARY)
    fig.suptitle(
        f"M = {M}, K = {K}, {MOD_LABEL[modulation]} ({residual} residual): SNR loss at BER "
        f"1e-3\nbold: within the {LOSS_BUDGET_DB} dB budget;  ∞: a floor (never reaches 1e-3)",
        color=INK,
    )
    return _save(fig, path)


def _narrowest(losses: list[dict], metric: str) -> tuple[dict, set]:
    """``{(modulation, M, K, nit): (W, g_s)}`` of the narrowest design (W, then g_s) within the
    budget on ``metric``, and the (modulation, M, K, nit) whose float CG floors."""
    best: dict[tuple, tuple[int, int]] = {}
    cg_floor = set()
    for r in losses:
        if r["residual"] != "recurrence" or r["nit"] == "":
            continue
        key = (r["modulation"], int(r["M"]), int(r["K"]), int(r["nit"]))
        if r["detector"].startswith("cg") and r["status"] == FLOOR:
            cg_floor.add(key)
        in_budget = r[metric] != "" and float(r[metric]) <= LOSS_BUDGET_DB
        if r["detector"].startswith("fx:") and in_budget:
            design = (int(r["W"]), int(r["g_s"]))
            best[key] = min(best.get(key, design), design)
    return best, cg_floor


def _min_width_heatmap(losses: list[dict], path: Path) -> Path:
    widths = sorted({int(r["W"]) for r in losses if r["W"] != ""})
    nits = sorted({int(r["nit"]) for r in losses if r["nit"] != ""})
    configs = [(M, K) for K in K_VALUES for M in M_VALUES]
    cmap = ListedColormap(HEAT[: len(widths)]).with_extremes(bad=NEUTRAL)
    # One bin per width (W is even), centred on it.
    norm = BoundaryNorm([widths[0] - 1] + [w + 1 for w in widths], len(widths))
    metrics = (
        ("loss_mmse_db", "within 0.5 dB of float exact MMSE"),
        ("loss_cg_db", "within 0.5 dB of float CG, same nit"),
    )
    fig, axes = plt.subplots(
        len(MODULATIONS),
        len(metrics),
        figsize=(7.0, 8.4),
        sharex=True,
        sharey=True,
        layout="constrained",
    )
    for col, (metric, label) in enumerate(metrics):
        best, cg_floor = _narrowest(losses, metric)
        for row, mod in enumerate(MODULATIONS):
            ax = axes[row][col]
            V = np.full((len(configs), len(nits)), np.nan)
            for i, (M, K) in enumerate(configs):
                for j, n in enumerate(nits):
                    design = best.get((mod, M, K, n))
                    if n <= K and design is not None:
                        V[i, j] = design[0]
            im = ax.imshow(np.ma.masked_invalid(V), cmap=cmap, norm=norm, aspect="auto")
            for i, (M, K) in enumerate(configs):
                for j, n in enumerate(nits):
                    if n > K:  # no such design: blank to the surface
                        ax.add_patch(
                            plt.Rectangle(
                                (j - 0.5, i - 0.5), 1, 1, color=SURFACE, zorder=2
                            )
                        )
                        continue
                    design = best.get((mod, M, K, n))
                    if design is not None:
                        W, g = design
                        _cell_text(ax, i, j, f"{W}/{g}", widths.index(W))
                    elif metric == "loss_cg_db" and (mod, M, K, n) in cg_floor:
                        _cell_text(ax, i, j, "F", None)
                    else:
                        _cell_text(ax, i, j, "—", None)
            ax.set_xticks(range(len(nits)), [str(n) for n in nits])
            ax.set_yticks(range(len(configs)), [f"{M}×{K}" for M, K in configs])
            ax.grid(False)
            if row == 0:
                ax.set_title(label, color=INK)
            if row == len(MODULATIONS) - 1:
                ax.set_xlabel("CG iterations")
            if col == 0:
                ax.set_ylabel(f"{MOD_LABEL[mod]}:  M × K")
    cbar = fig.colorbar(
        im,
        ax=list(axes.ravel()),
        spacing="uniform",
        fraction=0.04,
        pad=0.02,
        shrink=0.5,
    )
    cbar.set_ticks(widths)
    cbar.set_label("narrowest register width W (bits)")
    cbar.outline.set_edgecolor(INK_SECONDARY)
    fig.suptitle(
        "Narrowest fixed-point CG within 0.5 dB at BER 1e-3 (recurrence form); cell: W / g_s",
        color=INK,
    )
    fig.supxlabel(
        "—: no width in the sweep is within the budget.  F: float CG itself never reaches "
        "1e-3 at this nit.  Blank: nit > K.",
        fontsize=6.5,
        color=INK_SECONDARY,
    )
    return _save(fig, path)


def write_accuracy_figures(
    accuracy_grid_csv: os.PathLike,
    accuracy_losses_csv: os.PathLike,
    out_dir: os.PathLike,
) -> list[Path]:
    """Render every Phase 3 figure into ``out_dir``; return the paths."""
    curves = design_curves(read_table(accuracy_grid_csv))
    losses = read_table(accuracy_losses_csv)
    guards = sorted({int(r["g_s"]) for r in losses if r["g_s"] != ""})
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    with plt.rc_context(STYLE):
        for M, K, mod, residual in DETAIL_CASES:
            case = _case_key(M, K, mod, residual)
            tag = f"{M}x{K}_{mod}"
            written.append(
                _ber_family(curves, case, guards, out_dir / f"accuracy_ber_{tag}.svg")
            )
            written.append(
                _loss_heatmap(
                    losses, case, guards, out_dir / f"accuracy_loss_{tag}.svg"
                )
            )
        written.append(_min_width_heatmap(losses, out_dir / "accuracy_min_width.svg"))
    return written
