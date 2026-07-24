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

#: (attr, label, colour, rtl_kw) per stage, in dataflow order (top to bottom).  ``attr`` is the pysim
#: composite attribute; ``rtl_kw`` matches the RTL task-instance name in a trace (see run_rtl_timeline).
_STAGES = [
    ("rx",      "cmd_rx (framer)",      C_RX,      "cmd_rx"),
    ("rstream", "MemRStream (gmem0)",   C_MEMR,    "mem_r_stream"),
    ("load",    "il_load → SOB",        C_LOAD,    "il_load"),
    ("compute", "il_compute (gather)",  C_COMPUTE, "il_compute"),
    ("store",   "il_store (framer)",    C_STORE,   "il_store"),
    ("wstream", "MemWStream (gmem1)",   C_MEMW,    "mem_w_stream"),
]


def _cadence(ends: "list[float]") -> int:
    """Steady-state firing cadence: the median gap between consecutive firing end-times, skipping the
    first (pipeline fill).  This is the per-stage throughput — apples-to-apples between pysim and RTL,
    and it reflects the calibrated residuals (a mem-stream's trailing control delay lands in the gap,
    even though it is not inside the fire_log window)."""
    import numpy as np

    e = np.asarray(sorted(ends), dtype=float)
    if len(e) < 2:
        return 0
    gaps = np.diff(e)
    return int(round(float(np.median(gaps[1:] if len(gaps) > 1 else gaps))))


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

    stages = []
    for attr, label, colour, _kw in _STAGES:
        wins = [[round(s), round(e)] for s, e in getattr(il, attr).fire_log]
        stages.append({"label": label, "colour": colour, "windows": wins,
                       "firings": len(wins), "firings_per_job": round(len(wins) / max(1, n_jobs)),
                       "cadence_cyc": _cadence([w[1] for w in wins])})
    gaps = np.diff(np.asarray(il.gather.job_end_cyc))
    period = int(round(float(np.median(gaps[1:] if len(gaps) > 1 else gaps)))) if len(gaps) else 0
    return {"n_jobs": n_jobs, "n": n, "period_cyc": period, "stages": stages}


def run_rtl_timeline(vcd_path: "str | Path", *, mem_dwidth: int = 64, n: int = 256, n_jobs: int = 4) -> dict:
    """Extract each stage's per-firing timing from an **RTL trace** — the ground-truth counterpart to
    :func:`run_timeline`.  Rebuilds the trace manifest from the elaborated composite, binds it to the
    VCD, and reads every task's firings via :meth:`~waveflow.utils.trace.BoundTrace.component_firings`.

    Returns the same shape as the pysim timeline (``stages`` with ``cadence_cyc`` / ``firings_per_job``),
    plus each stage's median ``span_cyc`` (first-input → ap_done).  Needs the trace, not the toolchain."""
    import numpy as np

    from waveflow.build.composite_gen import composite_top_spec
    from waveflow.build.elaborate import elaborate
    from waveflow.utils.trace import load_trace

    from examples.interleaver.interleaver_inband import InterleaverInband

    comp = elaborate(InterleaverInband, {"mem_dwidth": mem_dwidth, "n": n})
    bt = load_trace(composite_top_spec(comp, width=mem_dwidth).trace_manifest(), vcd_path)
    insts = [t["inst"] for t in bt.manifest["tasks"]]

    stages, done_ends = [], []
    for _attr, label, colour, kw in _STAGES:
        inst = next((i for i in insts if kw in i), None)
        firings = bt.component_firings(inst) if inst else ()
        ends = [f.end for f in firings]
        spans = [f.span for f in firings]
        stages.append({"label": label, "colour": colour, "firings": len(firings),
                       "firings_per_job": round(len(firings) / max(1, n_jobs)),
                       "cadence_cyc": _cadence(ends),
                       "span_cyc": int(np.median(spans)) if spans else 0})
        if kw == "il_compute":
            done_ends = ends
    period = _cadence(done_ends)
    return {"n_jobs": n_jobs, "n": n, "period_cyc": period, "stages": stages}


def compare_timelines(pysim: dict, rtl: dict) -> dict:
    """Join the pysim and RTL timelines by stage into a **cadence** comparison — the apples-to-apples
    throughput per stage, plus the overall period and its agreement.

    Cadence (not per-firing span) is the comparable quantity: RTL ``component_firings`` anchors a span at
    the first *input* handshake while a pysim ``fire_log`` starts before the input `get`, so the two
    span definitions differ for paced stages — but the firing *cadence* is well-defined on both sides."""
    by_label = {s["label"]: s for s in rtl["stages"]}
    rows = []
    for ps in pysim["stages"]:
        rt = by_label.get(ps["label"], {})
        rows.append({"stage": ps["label"],
                     "firings_per_job": ps.get("firings_per_job"),
                     "rtl_cadence": rt.get("cadence_cyc"),
                     "pysim_cadence": ps.get("cadence_cyc")})
    pr, pp = rtl["period_cyc"], pysim["period_cyc"]
    err = round(100.0 * abs(pp - pr) / pr, 2) if pr else None
    return {"n": pysim.get("n"), "stages": rows,
            "period": {"rtl": pr, "pysim": pp, "abs_err_pct": err}}


def format_compare_markdown(cmp: dict) -> str:
    """Render :func:`compare_timelines` output as the Markdown table the docs embed."""
    lines = ["| stage | firings/job | RTL cadence | PySim cadence |",
             "|-------|:-----------:|:-----------:|:-------------:|"]
    for r in cmp["stages"]:
        lines.append(f"| `{r['stage']}` | {r['firings_per_job']} | {r['rtl_cadence']} | "
                     f"{r['pysim_cadence']} |")
    p = cmp["period"]
    lines.append(f"| **period (cyc/job)** | | **{p['rtl']}** | **{p['pysim']}** |")
    return "\n".join(lines) + f"\n\nOverall period agreement: **{100 - (p['abs_err_pct'] or 0):.1f}%** " \
                              f"(RTL {p['rtl']} vs PySim {p['pysim']} cyc/job)."


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


#: Markers in rtltiming.md between which the generated cadence table is written in place.
_TABLE_BEGIN = "<!-- timing-compare:begin -->"
_TABLE_END = "<!-- timing-compare:end -->"
_RTLTIMING_MD = _DOCS_IMAGES.parent / "rtltiming.md"


@dataclass(kw_only=True)
class InterleaverRtlTimingStep(BuildStep):
    """Build + trace the interleaver RTL, extract each stage's firing cadence, and compare it to the pysim
    timeline — the RTL-vs-pysim **cadence** table.

    **Toolchain-gated** (Vitis HLS csynth + Vivado xsim): unlike the activity figure (pysim, committed and
    CI-regenerable), the RTL trace needs the toolchain, so this is an on-demand ``-m xsi``-class rung. It
    **consumes** the pysim timeline, writes the comparison JSON to ``results/`` and the rendered table
    **in place** into ``rtltiming.md`` (between the ``timing-compare`` markers), so the doc's table is the
    step's output, not hand-maintained.
    """

    description: str = "Build+trace the interleaver RTL and compare stage cadences to the pysim timeline."
    params: ClassVar[dict] = {}
    n: int = 256
    n_jobs: int = 4

    @property
    def consumes(self) -> list:  # type: ignore[override]
        return ["interleaver_timeline"]

    @property
    def produces(self) -> dict:  # type: ignore[override]
        return {"timing_compare": Path("results/interleaver_timing_compare.json"),
                "rtltiming_md": _RTLTIMING_MD}

    def run(self, config: BuildConfig, **artifacts) -> dict[str, Any]:
        from examples.interleaver.measure_compute_spans import build_rtl_trace

        pysim = json.loads(Path(artifacts["interleaver_timeline"]).read_text(encoding="utf-8"))
        vcd = build_rtl_trace(sizes=(self.n,) * self.n_jobs, n_max=self.n, width=64)
        rtl = run_rtl_timeline(vcd, mem_dwidth=64, n=self.n, n_jobs=self.n_jobs)
        cmp = compare_timelines(pysim, rtl)

        out = config.root_dir / "results" / "interleaver_timing_compare.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(cmp, indent=2) + "\n", encoding="utf-8")
        _write_table_between_markers(_RTLTIMING_MD, format_compare_markdown(cmp))
        p = cmp["period"]
        print(f"[rtl-timing] period RTL {p['rtl']} vs PySim {p['pysim']} "
              f"({100 - (p['abs_err_pct'] or 0):.1f}%) -> {out}")
        return {"timing_compare": out, "rtltiming_md": _RTLTIMING_MD}


def _write_table_between_markers(md_path: Path, table: str) -> None:
    """Replace the region between the ``timing-compare`` markers in *md_path* with *table*."""
    text = Path(md_path).read_text(encoding="utf-8")
    if _TABLE_BEGIN not in text or _TABLE_END not in text:
        raise RuntimeError(f"{md_path} is missing the timing-compare markers")
    head, rest = text.split(_TABLE_BEGIN, 1)
    _mid, tail = rest.split(_TABLE_END, 1)
    Path(md_path).write_text(f"{head}{_TABLE_BEGIN}\n{table}\n{_TABLE_END}{tail}", encoding="utf-8")


def build_interleaver_figures_dag():
    """The pysim → figure DAG (+ the gated RTL-timing rung), driven through the standard :func:`run_dag_cli`.
    When the interleaver grows a full ``interleaver_build.py`` (with the codegen / rtlsim rungs the build-up
    doc pages need), these steps slot into it beside them — for now they stand alone."""
    from waveflow.build.build import BuildDag, SourceStep

    dag = BuildDag()
    dag.add(SourceStep(artifact="interleaver_source", path=HERE / "interleaver_inband.py"))
    dag.add(InterleaverPySimStep(name="pysim"))
    dag.add(InterleaverFiguresStep(name="figures"))
    dag.add(InterleaverRtlTimingStep(name="rtl_timing"))   # gated: needs Vitis HLS + Vivado xsim
    return dag


if __name__ == "__main__":
    from waveflow.build.cli import run_dag_cli

    run_dag_cli(
        build_interleaver_figures_dag,
        description="Run the interleaver pysim and render the activity figure(s) from its timeline.",
        default_through="figures",
        root_dir=HERE,
    )
