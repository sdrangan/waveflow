"""validate_figure.py — the held-out validation figure, rendered deterministically as SVG.

Step 5.8 of ``plans/mimo_cg/mimo_cg_paper_sims.md``.  One figure for the three AC5 quantities
measured on the full designs — LUT, FF and job cycles — each as two panels sharing an x axis:

* top: predicted against measured, on log axes, with the ``y = x`` line (does the model cover the
  range?);
* bottom: the signed error in percent against the measured value, with AC5's band shaded (how big
  are the errors, and do they grow with size?).

Form and encoding (the dataviz skill's procedure): the job is "delta to a target", so position
carries it — a baseline at zero and a shaded band — and one hue is enough, because every point is
the same kind of thing (a held-out design, or one of its jobs).  A single series needs no legend.
Marks are 8 px dots with a 2 px surface ring; grid lines are solid hairlines; text is ink, never
the data colour.  The counted quantities (DSP, BRAM) are numbers, not a chart: the heading states
them.  The table behind the figure is ``paper_data/model_validation.csv``.

Determinism: the Phase 1 style (fixed ``svg.hashsalt``, text as paths, no date), so a re-run
writes a byte-identical file.
"""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter, LogLocator, NullFormatter

from examples.mimo_cg.mimo_cg_figures import GRID, INK, INK_SECONDARY, STYLE, _save

#: The reference palette's blue, step 450: the one hue of the figure.
MARK = "#2a78d6"
SURFACE = "#fcfcfb"
#: The wash of AC5's error band: one step off the surface, like the grid.
BAND = "#f0efec"
#: Dot area in points² (8 px across) and its surface ring.
DOT = {"s": 36, "color": MARK, "edgecolors": SURFACE, "linewidths": 1.5, "zorder": 3}


def _compact(v: float, _pos=None) -> str:
    if v >= 1e6:
        return f"{v / 1e6:g}M"
    if v >= 1e3:
        return f"{v / 1e3:g}k"
    return f"{v:g}"


def _panel(top, bot, rows: list[dict], band: float, title: str) -> None:
    meas = [r["measured"] for r in rows]
    pred = [r["predicted"] for r in rows]
    err = [r["error_pct"] for r in rows]
    lo, hi = min(meas + pred) / 1.25, max(meas + pred) * 1.25
    # label 1-2-5 within a decade or two; decades only when the range is wider
    subs = (1, 2, 5) if hi / lo < 100 else (1,)
    top.plot([lo, hi], [lo, hi], color=INK_SECONDARY, linewidth=1.0, zorder=2)
    top.scatter(meas, pred, **DOT)
    top.set_xscale("log")
    top.set_yscale("log")
    top.set_xlim(lo, hi)
    top.set_ylim(lo, hi)
    top.set_title(title, color=INK)
    mean = sum(abs(e) for e in err) / len(err)
    top.text(
        0.04,
        0.96,
        f"mean error {mean:.1f}%\nworst {max(abs(e) for e in err):.1f}%",
        transform=top.transAxes,
        va="top",
        ha="left",
        fontsize=7,
        color=INK_SECONDARY,
    )
    bot.axhspan(-band, band, color=BAND, zorder=1)
    bot.axhline(0, color=INK_SECONDARY, linewidth=1.0, zorder=2)
    bot.scatter(meas, err, **DOT)
    span = max(band, max(abs(e) for e in err)) * 1.25
    bot.set_ylim(-span, span)
    bot.set_xlabel("measured")
    for ax in (top, bot):
        ax.xaxis.set_major_locator(LogLocator(base=10, subs=subs))
        ax.xaxis.set_major_formatter(FuncFormatter(_compact))
        ax.xaxis.set_minor_formatter(NullFormatter())
        ax.tick_params(length=0)
    top.yaxis.set_major_locator(LogLocator(base=10, subs=subs))
    top.yaxis.set_major_formatter(FuncFormatter(_compact))
    top.yaxis.set_minor_formatter(NullFormatter())
    top.tick_params(labelbottom=False)
    bot.text(
        0.98,
        0.94,
        f"AC5 band: ±{band:g}%",
        transform=bot.transAxes,
        va="top",
        ha="right",
        fontsize=6.5,
        color=INK_SECONDARY,
    )


def render(detail: list[dict], path: Path) -> Path:
    """Draw the figure from :func:`examples.mimo_cg.hw.validate.detail_rows` and write ``path``."""
    from examples.mimo_cg.hw.validate import MAX_AREA_MAPE, MAX_CYCLE_MAPE

    design = [r for r in detail if r["scope"] == "design"]
    panels = (
        ("LUT", [r for r in design if r["quantity"] == "lut"], MAX_AREA_MAPE),
        ("FF", [r for r in design if r["quantity"] == "ff"], MAX_AREA_MAPE),
        (
            "cycles per job",
            [r for r in design if r["quantity"].startswith("job_cycles")],
            MAX_CYCLE_MAPE,
        ),
    )
    blocks = sorted(
        {(r["build"], r["scope"]) for r in detail if r["scope"].startswith("block:")}
    )
    exact = {
        ctr: sum(
            r["exact"]
            for r in detail
            if r["scope"].startswith("block:") and r["quantity"] == ctr
        )
        for ctr in ("dsp", "bram")
    }
    n_designs = len({r["build"] for r in design})
    path.parent.mkdir(parents=True, exist_ok=True)
    with plt.rc_context(STYLE):
        fig, axes = plt.subplots(
            2,
            3,
            figsize=(7.0, 4.3),
            sharex="col",
            gridspec_kw={"height_ratios": [1.5, 1]},
        )
        for col, (title, rows, band) in enumerate(panels):
            _panel(axes[0][col], axes[1][col], rows, band, title)
        axes[0][0].set_ylabel("predicted")
        axes[1][0].set_ylabel("error (%)")
        for ax in axes.flat:
            ax.grid(True, which="major", color=GRID, linewidth=0.5)
            ax.grid(False, which="minor")
        fig.suptitle(
            f"Held-out validation: {n_designs} detectors the models never saw\n"
            f"DSP exact on {exact['dsp']} of {len(blocks)} block configurations, "
            f"BRAM on {exact['bram']} of {len(blocks)}",
            fontsize=8,
            color=INK,
        )
        fig.tight_layout()
        return _save(fig, path)
