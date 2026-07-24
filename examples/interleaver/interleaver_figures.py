"""interleaver_figures.py — activity-diagram views of the interleaver's pipeline timing.

The build DAG has two rungs. :class:`InterleaverPySimStep` runs the interleaver pysim with the platform's
full calibration, checks the gather golden, and writes the per-stage **timeline**
(``results/interleaver_pysim.json`` — each stage's per-firing ``fire_log`` windows). :class:`InterleaverFiguresStep`
then **consumes** that artifact and renders the six-stage pipeline-activity band diagram — it does not
re-run the sim.

Where mem_copy draws its figures from an RTL *trace*, the source here is the **pysim** timeline: it needs
no toolchain and is deterministic, so the committed SVG regenerates anywhere and a re-run is a no-op unless
the timeline moved. (The same `ActivityDiagram` could be fed an RTL trace's ``component_firings`` instead —
the ground-truth view — but that would be toolchain-gated, so the committed, CI-regenerable figure uses the
pysim timeline the design already reproduces to <1%.)

Run it::

    python examples/interleaver/interleaver_figures.py            # pysim -> timeline -> committed SVG
    python examples/interleaver/interleaver_figures.py --list-steps
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

from waveflow.build.build import BuildConfig, BuildStep

HERE = Path(__file__).resolve().parent

#: Committed docs figure — a deterministic SVG straight from the calibrated pysim (no toolchain), so the
#: docs page tracks the design and re-rendering is a no-op unless the timeline actually moved.
_DOCS_IMAGES = HERE.parents[1] / "docs" / "examples" / "interleaver" / "images"
#: The pysim timeline artifact the figure is drawn from (under the build root's gitignored results/).
_TIMELINE_JSON = "results/interleaver_pysim.json"

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


def run_timeline(n_jobs: int = 6, n: int = 256) -> dict:
    """Run the interleaver pysim with the platform's **full** calibration — bus law, the mem-stream
    reader/writer residuals, and the interleaver's own fitted compute model — check the gather golden
    (``run_interleaver`` asserts ``Y = X[P]``), and return the per-stage timeline::

        {"n_jobs", "n", "period_cyc", "stages": [{"label", "colour", "windows": [[start, end], …]}, …]}
    """
    import numpy as np

    from examples.interleaver.interleaver_sim import run_interleaver
    from waveflow.calib.platform import packaged_platforms_dir

    # The shipped reference platform gives the calibrated timeline: platform_dir loads the bus law + the
    # mem-stream residuals onto the mem stages, compute_calib_dir the fitted gather model.  Fall back to
    # plain per-word timing if the platform is absent.
    pkg = packaged_platforms_dir()
    plat = pkg / "zynq7020_bfm_100mhz" if pkg else None
    plat_dir = str(plat) if plat and plat.is_dir() else None
    comp_dir = str(plat / "components" / "il_compute_task") if plat_dir else None

    il = run_interleaver(nj=n_jobs, n=n, platform_dir=plat_dir, compute_calib_dir=comp_dir)

    stages = [{"label": label, "colour": colour,
               "windows": [[round(s), round(e)] for s, e in getattr(il, attr).fire_log]}
              for attr, label, colour in _STAGES]
    gaps = np.diff(np.asarray(il.gather.job_end_cyc))
    period = int(round(float(np.median(gaps[1:] if len(gaps) > 1 else gaps)))) if len(gaps) else 0
    return {"n_jobs": n_jobs, "n": n, "period_cyc": period, "stages": stages}


def _lanes_from_stages(stages: list[dict]):
    """Timeline stages → ActivityDiagram ``(label, event_cycles, colour)`` lanes.

    A stage that fires back-to-back would collapse to one solid bar; we trim a small (~2%, min 2-cycle)
    seam off each window's end so consecutive firings render as **one band per job** — you can count the
    jobs and read each stage's per-job stagger. The seam is a legibility device, not real idle."""
    import numpy as np

    lanes = []
    for st in stages:
        wins = st["windows"]
        if wins:
            runs = []
            for a, b in wins:
                seam = max(2, round(0.02 * (b - a)))
                runs.append(np.arange(a, max(a + 1, b - seam)))
            events = np.concatenate(runs)
        else:
            events = np.array([], dtype=int)
        lanes.append((st["label"], events, st["colour"]))
    return lanes


def render_from_timeline(timeline: dict, out: Path) -> None:
    """Render the six-stage pipeline-activity band diagram from a *timeline* dict (deterministic SVG)."""
    import matplotlib
    matplotlib.use("svg")
    matplotlib.rcParams["svg.hashsalt"] = "interleaver_figures"
    import matplotlib.pyplot as plt

    from waveflow.utils.timing import ActivityDiagram

    lanes = _lanes_from_stages(timeline["stages"])
    hi = int(max((e[-1] for _, e, _ in lanes if len(e)), default=1)) + 20
    nj = timeline["n_jobs"]
    ad = ActivityDiagram(lanes, time_unit="cycle")
    fig, _ax, _ = ad.plot(
        mode="band", trange=(0, hi), gap=1, fig_width=11, fig_height=3.4,
        title=f"interleaver in-band (framework mem-streams) — six-stage pipeline over {nj} jobs "
              "(one band = one job)")
    # Deterministic SVG (no embedded date, fixed id salt) so a committed figure only changes when the
    # timeline does — the mem_copy_figures convention.
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, format="svg", bbox_inches="tight", metadata={"Date": None})
    plt.close(fig)


def save_figures(output_dir: str | Path, n_jobs: int = 6, n: int = 256) -> list[Path]:
    """Convenience: run the pysim timeline and render it in one call (the toolchain-free path the test
    exercises). The build DAG splits these into :class:`InterleaverPySimStep` + :class:`InterleaverFiguresStep`."""
    out = Path(output_dir) / "pipeline_activity.svg"
    render_from_timeline(run_timeline(n_jobs=n_jobs, n=n), out)
    return [out]


@dataclass(kw_only=True)
class InterleaverPySimStep(BuildStep):
    """Run the interleaver pysim (fully calibrated), check the gather golden, and write the per-stage
    timeline — the artifact the figure is drawn from.

    The first, toolchain-free checkpoint (mem_copy's ``PySimStep`` role): correctness **and** the timeline
    in one run, so the figure step never re-runs the sim.
    """

    description: str = "Run the interleaver pysim golden and record the per-stage timeline."
    params: ClassVar[dict] = {}
    n_jobs: int = 6
    n: int = 256

    @property
    def consumes(self) -> list:  # type: ignore[override]
        return ["interleaver_source"]

    @property
    def produces(self) -> dict:  # type: ignore[override]
        return {"interleaver_timeline": Path(_TIMELINE_JSON)}

    def run(self, config: BuildConfig, **_) -> dict[str, Any]:
        timeline = run_timeline(n_jobs=self.n_jobs, n=self.n)     # run_interleaver asserts Y = X[P]
        out = config.root_dir / _TIMELINE_JSON
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(timeline, indent=2) + "\n", encoding="utf-8")
        print(f"[pysim] {timeline['n_jobs']} jobs, gather golden bit-exact, "
              f"period={timeline['period_cyc']} cyc/job -> {out}")
        return {"interleaver_timeline": out}


@dataclass(kw_only=True)
class InterleaverFiguresStep(BuildStep):
    """Render the six-stage pipeline-activity figure **from the pysim timeline artifact** into the committed
    docs images (``docs/examples/interleaver/images/``).

    It **consumes** :class:`InterleaverPySimStep`'s timeline — it does not re-run the sim. Where mem_copy's
    ``TimingFiguresStep`` consumes an RTL *trace*, this consumes the pysim timeline: deterministic and
    toolchain-free, so the committed SVG regenerates anywhere and a re-run is a no-op unless the timeline
    moved (the git diff is the review signal).
    """

    description: str = "Render the interleaver pipeline-activity figure from the pysim timeline."
    params: ClassVar[dict] = {}

    @property
    def consumes(self) -> list:  # type: ignore[override]
        return ["interleaver_timeline"]

    @property
    def produces(self) -> dict:  # type: ignore[override]
        return {"pipeline_activity_svg": _DOCS_IMAGES / "pipeline_activity.svg"}

    def run(self, config: BuildConfig, **artifacts) -> dict[str, Any]:
        timeline = json.loads(Path(artifacts["interleaver_timeline"]).read_text(encoding="utf-8"))
        out = _DOCS_IMAGES / "pipeline_activity.svg"
        render_from_timeline(timeline, out)
        return {"pipeline_activity_svg": out}


def build_interleaver_figures_dag():
    """The pysim → figure DAG, driven through the standard :func:`run_dag_cli`.  When the interleaver grows
    a full ``interleaver_build.py`` (with the codegen / rtlsim rungs the build-up doc pages need), these
    steps slot into it beside them — for now they stand alone."""
    from waveflow.build.build import BuildDag, SourceStep

    dag = BuildDag()
    dag.add(SourceStep(artifact="interleaver_source", path=HERE / "interleaver_inband.py"))
    dag.add(InterleaverPySimStep(name="pysim"))
    dag.add(InterleaverFiguresStep(name="figures"))
    return dag


if __name__ == "__main__":
    from waveflow.build.cli import run_dag_cli

    run_dag_cli(
        build_interleaver_figures_dag,
        description="Run the interleaver pysim and render the activity figure(s) from its timeline.",
        default_through="figures",
        root_dir=HERE,
    )
