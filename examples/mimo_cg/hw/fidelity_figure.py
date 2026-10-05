"""fidelity_figure.py — the decision-fidelity and learning-curve figures, as deterministic SVG.

Steps 6.5 and 6.6 of ``plans/mimo_cg/mimo_cg_paper_sims.md``.

* ``decision_fidelity.svg`` — one panel per resource.  Each dot is a decision: the measured cost of
  the design the model picked against the measured cost of the brute-force pick.  A dot on the
  line is the same cost; the shaded band is AC6's 10%.  The job is "delta to a target", so position
  carries it.
* ``learning_curve.svg`` — one panel per resource.  The share of decisions that are right against
  the number of calibration builds the models were fitted on: the range over the refits as a thin
  bar, their median as a dot, and AC6's 90% as a line.

Form and encoding (the dataviz skill's procedure): the four resources are small multiples with one
hue, so no categorical palette and no legend are needed; a decision that is not right is drawn
hollow, so it does not rest on colour; text is ink, never the data colour.  The tables behind the
figures are ``paper_data/decision_fidelity.csv`` and ``learning_curve.csv``.

Determinism: the Phase 1 style (fixed ``svg.hashsalt``, text as paths, no date), so a re-run
writes byte-identical files.
"""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter, LogLocator, NullFormatter

from examples.mimo_cg.hw.validate_figure import BAND, MARK, SURFACE, _compact
from examples.mimo_cg.mimo_cg_figures import GRID, INK, INK_SECONDARY, STYLE, _save

RESOURCES = ("dsp", "lut", "ff", "bram")
LABEL = {"dsp": "DSP", "lut": "LUT", "ff": "FF", "bram": "block RAM"}
RIGHT = {"s": 26, "color": MARK, "edgecolors": SURFACE, "linewidths": 1.0, "zorder": 3}
WRONG = {
    "s": 30,
    "facecolors": "none",
    "edgecolors": INK,
    "linewidths": 1.2,
    "zorder": 4,
}


def render_decisions(rows: list[dict], metrics: list[dict], path: Path) -> Path:
    """Draw the decisions of ``fidelity.score`` (``rows``) with the headline numbers of
    ``fidelity.metrics`` (``metrics``, the ``all`` set) and write ``path``."""
    from examples.mimo_cg.hw.fidelity import COST_TOLERANCE, MIN_RIGHT_PCT

    head = {m["resource"]: m for m in metrics if m["set"] == "all"}
    path.parent.mkdir(parents=True, exist_ok=True)
    with plt.rc_context(STYLE):
        fig, axes = plt.subplots(1, 4, figsize=(7.0, 2.6))
        for ax, res in zip(axes, RESOURCES, strict=True):
            mine = [
                r
                for r in rows
                if r["resource"] == res and r["measured"] and r["bf_cost"] != ""
            ]
            x = [float(r["bf_cost"]) for r in mine]
            y = [float(r["m_cost"]) for r in mine]
            lo, hi = min(x + y) / 1.3, max(x + y) * 1.3
            ax.fill_between(
                [lo, hi],
                [lo, hi],
                [lo * (1 + COST_TOLERANCE), hi * (1 + COST_TOLERANCE)],
                color=BAND,
                zorder=1,
                linewidth=0,
            )
            ax.plot([lo, hi], [lo, hi], color=INK_SECONDARY, linewidth=1.0, zorder=2)
            ok = [(a, b) for a, b, r in zip(x, y, mine, strict=True) if r["right"]]
            bad = [(a, b) for a, b, r in zip(x, y, mine, strict=True) if not r["right"]]
            if ok:
                ax.scatter(*zip(*ok, strict=True), **RIGHT)
            if bad:
                ax.scatter(*zip(*bad, strict=True), **WRONG)
            ax.set_xscale("log")
            ax.set_yscale("log")
            ax.set_xlim(lo, hi)
            ax.set_ylim(lo, hi)
            subs = (1, 2, 5) if hi / lo < 100 else (1,)
            for axis in (ax.xaxis, ax.yaxis):
                axis.set_major_locator(LogLocator(base=10, subs=subs))
                axis.set_major_formatter(FuncFormatter(_compact))
                axis.set_minor_formatter(NullFormatter())
            ax.tick_params(length=0)
            ax.grid(True, which="major", color=GRID, linewidth=0.5)
            ax.grid(False, which="minor")
            m = head[res]
            ax.set_title(LABEL[res], color=INK)
            ax.text(
                0.05,
                0.95,
                f"{float(m['right_pct']):.1f}% right\n"
                f"{float(m['same_cost_pct']):.1f}% at the best cost\n"
                f"worst +{float(m['regret_max_pct']):.1f}%",
                transform=ax.transAxes,
                va="top",
                ha="left",
                fontsize=6.5,
                color=INK_SECONDARY,
            )
            ax.set_xlabel("brute-force pick")
        axes[0].set_ylabel("model's pick (measured)")
        n = int(head["any"]["decisions"]) if "any" in head else len(rows)
        fig.suptitle(
            f"{n:,} decisions on the 1,440-detector sub-grid: the cost of the model's pick "
            f"against the best there is\n"
            f"filled: right (within the job-time budget and 10% of the best cost; AC6 asks "
            f"{MIN_RIGHT_PCT:g}%)   hollow: not right",
            fontsize=7.5,
            color=INK,
        )
        fig.tight_layout()
        return _save(fig, path)


def render_curve(rows: list[dict], path: Path) -> Path:
    """Draw the learning curve of ``fidelity.learning_curve`` and write ``path``."""
    from examples.mimo_cg.hw.fidelity import MIN_RIGHT_PCT

    sizes = sorted({int(r["builds"]) for r in rows})
    pos = {n: i for i, n in enumerate(sizes)}
    path.parent.mkdir(parents=True, exist_ok=True)
    with plt.rc_context(STYLE):
        fig, axes = plt.subplots(1, 4, figsize=(7.0, 2.5), sharey=True)
        for ax, res in zip(axes, RESOURCES, strict=True):
            med = []
            for n in sizes:
                got = sorted(
                    float(r["right_pct"])
                    for r in rows
                    if r["resource"] == res and int(r["builds"]) == n
                )
                med.append(got[len(got) // 2])
                if len(got) > 1:
                    ax.plot(
                        [pos[n], pos[n]],
                        [got[0], got[-1]],
                        color=MARK,
                        linewidth=2.0,
                        alpha=0.35,
                        solid_capstyle="round",
                        zorder=2,
                    )
            ax.axhline(MIN_RIGHT_PCT, color=INK_SECONDARY, linewidth=1.0, zorder=1)
            ax.plot(list(pos.values()), med, color=MARK, linewidth=2.0, zorder=3)
            ax.scatter(
                list(pos.values()),
                med,
                s=36,
                color=MARK,
                edgecolors=SURFACE,
                linewidths=1.5,
                zorder=4,
            )
            ax.set_xticks(list(pos.values()))
            ax.set_xticklabels([str(n) for n in sizes])
            ax.set_xlim(-0.5, len(sizes) - 0.5)
            ax.set_ylim(0, 104)
            ax.set_title(LABEL[res], color=INK)
            ax.set_xlabel("calibration builds")
            ax.tick_params(length=0)
            ax.grid(True, which="major", color=GRID, linewidth=0.5)
        axes[0].set_ylabel("decisions right (%)")
        axes[-1].text(
            len(sizes) - 0.55,
            MIN_RIGHT_PCT - 3,
            f"AC6: {MIN_RIGHT_PCT:g}%",
            va="top",
            ha="right",
            fontsize=6.5,
            color=INK_SECONDARY,
        )
        draws = max(int(r["draw"]) for r in rows) + 1
        fig.suptitle(
            "Decisions right on the sub-grid, by the number of builds the models were "
            "fitted on\n"
            f"dot: median of {draws} random subsets   bar: their range   "
            f"last point: all builds, the committed models",
            fontsize=7.5,
            color=INK,
        )
        fig.tight_layout()
        return _save(fig, path)
