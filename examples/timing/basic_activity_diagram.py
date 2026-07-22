"""
basic_activity_diagram.py - canonical runnable example for ``ActivityDiagram``.

Where ``basic_timing_diagram.py`` shows a *waveform* (per-transition value boxes at a ~10-cycle
zoom), this shows an *activity diagram*: labeled lanes on a common cycle axis whose content is
"when was this lane busy" rather than "what value did it hold".  That is the view that stays legible
across hundreds or thousands of cycles, so it is what you reach for to see how stages overlap and
where a pipeline stalls.

The data here is hand-authored (a few numpy cycle lists), not read from a trace -- the point is the
renderer, not any particular design.  In practice you would usually build the lanes from a run with
``ActivityDiagram.from_trace(bt, spec)``; see ``docs/examples/memcpy/timing.md``.

Usage
-----
Run from the repository root to regenerate the docs asset::

    python examples/timing/basic_activity_diagram.py

Or specify a custom output directory::

    python examples/timing/basic_activity_diagram.py --output /path/to/dir
"""

from __future__ import annotations

import argparse
from pathlib import Path

# Default destination keeps generated figures next to the docs page that references them.
_DEFAULT_OUTPUT_DIR = Path(__file__).parent.parent.parent / "docs" / "guide" / "_static" / "timing"

# ---------------------------------------------------------------------------
# Colour palette
# ---------------------------------------------------------------------------
# One colour per pipeline *stage*, reused for every lane that belongs to that stage.  Sharing a
# colour across related lanes is what lets a reader carry the mapping from one figure to the next.
# These are the Vega / Tableau-10 qualitative hues -- a good categorical default: distinct in hue,
# similar in lightness, colour-blind-friendlier than the primary colours.
C_LOAD, C_COMPUTE, C_STORE = "#4C78A8", "#F58518", "#54A24B"


def save_activity_figures(output_dir: str | Path) -> list[Path]:
    """Build a toy activity diagram and save the figure to *output_dir*.

    The example models a tiny ``load -> compute -> store`` accelerator running four jobs on a
    20-cycle cadence, with a small FIFO between ``compute`` and ``store``.  ``compute`` produces
    faster than ``store`` drains, so the FIFO fills and pins at capacity -- the shaded band in the
    occupancy panel, which is the backpressure made visible.

    Parameters
    ----------
    output_dir:
        Directory where the PNG file will be written.  Created if it does not already exist.

    Returns
    -------
    list[Path]
        Paths to the saved figure files.
    """
    import matplotlib
    matplotlib.use("Agg")  # non-interactive backend - safe for scripts and CI
    import matplotlib.pyplot as plt
    import numpy as np

    from waveflow.utils.timing import ActivityDiagram

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Build the per-lane event cycles.  A lane is just the (sorted) cycles at which it was active;
    # here we lay four jobs out on a fixed cadence.
    # ------------------------------------------------------------------
    jobs, period, n_cycles = 4, 20, 4 * 20

    def busy(offset: int, width: int) -> np.ndarray:
        """Cycles [offset, offset+width) within every job -- one contiguous run per job."""
        return np.concatenate(
            [np.arange(j * period + offset, j * period + offset + width) for j in range(jobs)])

    load_ev = busy(0, 4)      # a short read burst at the top of each job
    compute_ev = busy(3, 12)  # the long pole: compute runs most of the job
    store_ev = busy(11, 6)    # a write burst near the end

    lanes = [
        ("load",    load_ev,    C_LOAD),
        ("compute", compute_ev, C_COMPUTE),
        ("store",   store_ev,   C_STORE),
    ]

    # ------------------------------------------------------------------
    # A small FIFO between compute and store.  compute pushes a word each cycle it runs, store pops
    # one each cycle it runs; the level is clamped at the capacity, and every cycle it sits there is
    # a cycle compute was blocked.
    # ------------------------------------------------------------------
    cap = 4
    level = np.zeros(n_cycles, dtype=int)
    lvl = 0
    push, pop = set(compute_ev.tolist()), set(store_ev.tolist())
    for t in range(n_cycles):
        if t in push and lvl < cap:
            lvl += 1
        if t in pop and lvl > 0:
            lvl -= 1
        level[t] = lvl

    # ------------------------------------------------------------------
    # Assemble and render.  'band' collapses each lane's active cycles into contiguous bars; the
    # occupancy sub-panel is attached with set_occupancy and drawn beneath the lanes.
    # ------------------------------------------------------------------
    ad = ActivityDiagram(lanes, time_unit="cycle")
    ad.set_occupancy(level, cap, colour=C_COMPUTE, ylabel="fifo\noccupancy")
    fig, ax, ax2 = ad.plot(
        mode="band", trange=(0, n_cycles), fig_width=9, fig_height=3.6,
        title="toy load → compute → store accelerator (bar = busy)")

    out_path = output_dir / "basic_activity_diagram.png"
    fig.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close(fig)

    return [out_path]


# ---------------------------------------------------------------------------
# CLI entry-point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Generate the basic activity diagram figure into an output directory.")
    parser.add_argument(
        "--output",
        default=str(_DEFAULT_OUTPUT_DIR),
        help=f"Directory to write figure(s) into (default: {_DEFAULT_OUTPUT_DIR})")
    args = parser.parse_args()

    saved = save_activity_figures(args.output)
    for p in saved:
        print(f"Saved: {p}")
