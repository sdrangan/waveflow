"""interleaver_figures.py — activity-diagram views of the interleaver's pipeline timing.

Unlike ``mem_copy_figures`` (which reads an RTL trace), these render straight from the **pysim**
timeline — each stage's per-firing ``fire_log`` window — so they need no toolchain: the loosely-timed
model already charges the platform bus law (on the memory) and the custom gather's loop model (on
``il_compute``), and the figure shows how the six stages overlap across jobs.

The framework `MemRStream`/`MemWStream` compose the read/write stages; the headline figure is the
six-stage **pipeline activity** band diagram.

Run it::

    python examples/interleaver/interleaver_figures.py            # -> results/pipeline_activity.png
"""
from __future__ import annotations

import argparse
from pathlib import Path

_DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parent / "results"

# One colour per stage, grouped by role: token/framer (purple), the read side (blues), the custom
# compute (orange — it stands out because it is the one stage you calibrate yourself), the store side
# (greens).
C_RX, C_MEMR, C_LOAD = "#B279A2", "#4C78A8", "#72B7B2"
C_COMPUTE = "#F58518"
C_STORE, C_MEMW = "#54A24B", "#9E765F"

#: (attr, label, colour) per stage, in dataflow order (top to bottom).
_STAGES = [
    ("rx",      "cmd_rx (framer)",      C_RX),
    ("rstream", "MemRStream (gmem0)",   C_MEMR),
    ("load",    "il_load → SOB",        C_LOAD),
    ("compute", "il_compute (gather)",  C_COMPUTE),
    ("store",   "il_store (framer)",    C_STORE),
    ("wstream", "MemWStream (gmem1)",   C_MEMW),
]


def _stage_lanes(il, stages):
    """Turn each stage's ``fire_log`` windows into ActivityDiagram ``(label, event_cycles, colour)``
    lanes — the cycles a stage was busy.

    A stage that fires back-to-back (the always-busy mem stages) would otherwise collapse to one solid
    bar; we trim a small (~2%, min 2-cycle) seam off each window's end so consecutive firings render as
    **one band per job** — you can count the jobs and read each stage's per-job stagger, which is the
    pipeline overlap the figure is about. The seam is a legibility device, not real idle."""
    import numpy as np

    lanes = []
    for attr, label, colour in stages:
        wins = getattr(il, attr).fire_log
        if wins:
            runs = []
            for s, e in wins:
                a, b = round(s), round(e)
                seam = max(2, round(0.02 * (b - a)))
                runs.append(np.arange(a, max(a + 1, b - seam)))
            events = np.concatenate(runs)
        else:
            events = np.array([], dtype=int)
        lanes.append((label, events, colour))
    return lanes


def render_pipeline_activity(il, out: Path, n_jobs: int, stages, title: str) -> None:
    """The six stages' activity across the run — the pipeline overlap at a glance."""
    import matplotlib.pyplot as plt

    from waveflow.utils.timing import ActivityDiagram

    lanes = _stage_lanes(il, stages)
    hi = int(max((e[-1] for _, e, _ in lanes if len(e)), default=1)) + 20
    ad = ActivityDiagram(lanes, time_unit="cycle")
    fig, _ax, _ = ad.plot(
        mode="band", trange=(0, hi), gap=1, fig_width=11, fig_height=3.4, title=title)
    fig.savefig(out, dpi=150, bbox_inches="tight")
    plt.close(fig)


def save_figures(output_dir: str | Path, n_jobs: int = 6, n: int = 256) -> list[Path]:
    """Run the interleaver pysim (with the shipped platform bus law + the seeded compute model) and
    render the six-stage pipeline-activity figure into *output_dir*."""
    import matplotlib
    matplotlib.use("Agg")

    from examples.interleaver.interleaver_sim import run_interleaver
    from waveflow.calib.platform import packaged_platforms_dir

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Use the shipped reference platform's bus law when it resolves, so the timeline is the calibrated
    # one; fall back to the plain per-word timing if it is absent.
    pkg = packaged_platforms_dir()
    plat_dir = str(pkg / "zynq7020_bfm_100mhz") if pkg and (pkg / "zynq7020_bfm_100mhz").is_dir() else None

    il = run_interleaver(nj=n_jobs, n=n, platform_dir=plat_dir)   # default InterleaverInband

    out = output_dir / "pipeline_activity.png"
    render_pipeline_activity(
        il, out, n_jobs=n_jobs, stages=_STAGES,
        title=f"interleaver in-band (framework mem-streams) — six-stage pipeline over {n_jobs} jobs "
              "(one band = one job)")
    return [out]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Render the interleaver activity-diagram figure(s).")
    parser.add_argument("--output", default=str(_DEFAULT_OUTPUT_DIR),
                        help=f"output directory (default: {_DEFAULT_OUTPUT_DIR})")
    args = parser.parse_args()
    for p in save_figures(args.output):
        print(f"Saved: {p}")
