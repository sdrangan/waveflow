"""Committed-figure workflow for the ``mem_copy`` timing instrumentation.

Three figures, in the order the story is told on
``docs/examples/memcpy/timing.md``:

* **timeline_full.svg** — the whole run: every observation point on one cycle axis.
  The 183-cycle cadence and the read/write overlap are visible at a glance.
* **timeline_job.svg** — one steady-state job, beat by beat, with the ``copy_data``
  FIFO occupancy underneath.  This is where the 30 cycles of backpressure become a
  thing you can *see*: the channel pinned at capacity while the writer drains.
* **firing_spans.svg** — per-firing spans per component.  The writer is uniform at
  183 (100% utilised, the bottleneck); the reader is 153 on its one uncontended
  firing and 183 on every later one.

The first two need the waveform (per-beat detail); the third is rendered from
``results/mem_copy_timing.json`` alone, so it regenerates with no toolchain at all.

Generated SVGs land in ``results/`` (gitignored); :class:`SyncDocsFiguresStep`
promotes them — on demand, via an explicit manifest — into
``docs/examples/memcpy/images/`` as committed assets, so a docs figure only changes
when you intend it to and the change is a reviewable ``git diff``.

Run it with::

    python mem_copy_build.py --through sync_docs_figures
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

import numpy as np

import matplotlib

matplotlib.use("svg")
# Deterministic SVG: a stable hashsalt fixes the element ids matplotlib would otherwise randomize,
# so a re-render only diffs when the figure truly changed.
matplotlib.rcParams["svg.hashsalt"] = "mem_copy_figures"
import matplotlib.pyplot as plt  # noqa: E402

from waveflow.build.build import BuildConfig, BuildStep  # noqa: E402
from waveflow.utils.timing import ActivityDiagram  # noqa: E402

FIGURE_MANIFEST = [
    {"name": "timeline_full", "source": "results/timeline_full.svg",
     "dest": "docs/examples/memcpy/images/timeline_full.svg"},
    {"name": "timeline_job", "source": "results/timeline_job.svg",
     "dest": "docs/examples/memcpy/images/timeline_job.svg"},
    {"name": "firing_spans", "source": "results/firing_spans.svg",
     "dest": "docs/examples/memcpy/images/firing_spans.svg"},
]

#: One colour per stage of the pipeline, reused across all three figures so a reader can carry the
#: mapping from one to the next.
C_SEQ, C_READ, C_COPY, C_WRITE, C_DONE = (
    "#4C78A8", "#E45756", "#F58518", "#54A24B", "#B279A2")


def _save_svg(fig, path: Path) -> None:
    """Write a deterministic SVG (no embedded timestamp)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, format="svg", bbox_inches="tight", metadata={"Date": None})
    plt.close(fig)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# Reading the trace
# ---------------------------------------------------------------------------

def _observation_spec():
    """The lanes both timeline figures draw, top to bottom: commands in, through the two internal
    framed FIFOs and both memory ports, to the response out.  Ordered so the figure reads as
    dataflow -- a beat entering at the top exits at the bottom.

    A spec for :meth:`ActivityDiagram.from_trace`: ``(label, source, colour)`` per lane, each
    *source* naming a boundary port or one end of an internal channel.  The topology is mem_copy's;
    the walking of it into event lanes is the renderer's."""
    return [
        ("s_cmd in",      ("port", "s_cmd", "tvalid", "tready"),      C_SEQ),
        ("cmd write",     ("chan", "cmd", "write"),                   C_SEQ),
        ("cmd read",      ("chan", "cmd", "read"),                    C_SEQ),
        ("gmem0 AR",      ("port", "gmem0", "ARVALID", "ARREADY"),    C_READ),
        ("gmem0 R",       ("port", "gmem0", "RVALID", "RREADY"),      C_READ),
        ("copy_data wr",  ("chan", "copy_data", "write"),             C_COPY),
        ("copy_data rd",  ("chan", "copy_data", "read"),              C_COPY),
        ("gmem1 AW",      ("port", "gmem1", "AWVALID", "AWREADY"),    C_WRITE),
        ("gmem1 W",       ("port", "gmem1", "WVALID", "WREADY"),      C_WRITE),
        ("s_done out",    ("port", "s_done", "tvalid", "tready"),     C_DONE),
    ]


# ---------------------------------------------------------------------------
# Figures
# ---------------------------------------------------------------------------

def render_timeline_full(bt, out: Path, n_cycles: int | None = None) -> None:
    """Every stage's activity across the run, one lane per observation point.

    The whole run at band resolution: :class:`ActivityDiagram` collapses each lane's handshake
    cycles into contiguous busy bars, which carry the same information (when each stage was busy) as
    a per-beat plot at a fraction of the SVG paths.  The per-beat view is the job figure's job.
    """
    ad = ActivityDiagram.from_trace(bt, _observation_spec())
    hi = n_cycles or int(max((e[-1] for _, e, _ in ad.lanes if len(e)), default=1)) + 40
    fig, _, _ = ad.plot(
        mode="band", trange=(0, hi), fig_height=4.2,
        title="mem_copy — stage activity across 16 jobs (bar = busy)")
    _save_svg(fig, out)


def render_timeline_job(bt, out: Path, job: int = 8) -> None:
    """One steady-state firing, beat by beat, with the copy_data FIFO occupancy underneath.

    The occupancy panel is the point of the figure: `copy_data` sits at capacity for ~30 cycles at
    the start of the firing, which is the reader blocked on a writer that has not drained yet.  A
    write-enable-based metric shows nothing there -- HLS gates the enable -- so the counter is the
    only place that congestion is visible.
    """
    firings = bt.component_firings("mem_r_stream_framed_task")
    f = firings[min(job, len(firings) - 1)]
    lo, hi = f.start - 6, f.end + 40

    ad = ActivityDiagram.from_trace(bt, _observation_spec())
    level, cap = ActivityDiagram.occupancy_from_trace(bt, "copy_data")
    ad.set_occupancy(level, cap, colour=C_COPY, ylabel="copy_data\noccupancy")
    fig, _, _ = ad.plot(
        mode="beat", trange=(lo, hi), fig_height=5.2,
        title=f"one steady-state job (reader firing {f.index}: "
              f"cycles {f.start}–{f.end}, span {f.span})")
    _save_svg(fig, out)


def render_firing_spans(events: dict, out: Path) -> None:
    """Per-firing span for each component -- rendered from the timing table alone, no waveform.

    Shows the bottleneck without any arithmetic: the writer is a flat 183 on every firing, so it is
    busy for the entire period; the reader is 153 on the one firing nothing blocked it, and 183
    afterwards -- the 30-cycle difference is congestion, not its own cost.
    """
    by_comp: dict[str, list[dict]] = {}
    for f in events["firings"]:
        by_comp.setdefault(f["component"], []).append(f)

    order = ["mem_seq_framed_task", "mem_r_stream_framed_task", "mem_w_stream_framed_done_task"]
    comps = [c for c in order if c in by_comp] + [c for c in by_comp if c not in order]
    colours = {"mem_seq_framed_task": C_SEQ, "mem_r_stream_framed_task": C_READ,
               "mem_w_stream_framed_done_task": C_WRITE}

    fig, ax = plt.subplots(figsize=(11, 3.6))
    width = 0.8 / max(len(comps), 1)
    for i, comp in enumerate(comps):
        rows = sorted(by_comp[comp], key=lambda r: r["index"])
        xs = np.arange(len(rows)) + i * width - 0.4 + width / 2
        own = [r["span"] - r["blocked"] for r in rows]
        blocked = [r["blocked"] for r in rows]
        ax.bar(xs, own, width=width, color=colours.get(comp, "0.5"),
               label=comp.replace("_framed_task", "").replace("_framed_done_task", ""))
        # Stacked in grey rather than hatched: the blocked part is not this component's work, and a
        # translucent hatch over a coloured bar reads as a third colour rather than as "waiting".
        ax.bar(xs, blocked, width=width, bottom=own, color="0.78",
               edgecolor=colours.get(comp, "0.5"), linewidth=0.4,
               label="blocked (waiting on a full channel)" if i == 0 else None)

    ax.set_xticks(np.arange(len(next(iter(by_comp.values())))))
    ax.set_xlabel("firing (job index)")
    ax.set_ylabel("span (cycles)")
    ax.set_title("per-firing span — colour is the component's own work, grey is waiting",
                 fontsize=10)
    ax.legend(fontsize=8, frameon=False, ncol=4, loc="upper center",
              bbox_to_anchor=(0.5, -0.22))
    ax.grid(axis="y", alpha=0.25, linewidth=0.4)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    _save_svg(fig, out)


# ---------------------------------------------------------------------------
# Build steps
# ---------------------------------------------------------------------------

@dataclass(kw_only=True)
class TimingFiguresStep(BuildStep):
    """Render the three timing figures into ``results/``.

    Consumes the manifest and the waveform (per-beat detail the summary table does not carry) plus
    the extracted table (which ``firing_spans`` alone is enough for).
    """

    description: str = "Render the mem_copy timing figures from the traced run."
    params: ClassVar[dict] = {}

    manifest_artifact: str = "trace_manifest"
    vcd_artifact: str = "trace_vcd"
    events_artifact: str = "timing_events"
    zoom_job: int = 8

    @property
    def consumes(self) -> list:  # type: ignore[override]
        return [self.manifest_artifact, self.vcd_artifact, self.events_artifact]

    @property
    def produces(self) -> dict:  # type: ignore[override]
        return {f"{e['name']}_svg": Path(e["source"]) for e in FIGURE_MANIFEST}

    def run(self, config: BuildConfig, **artifacts) -> dict[str, Any]:
        from waveflow.utils.trace import load_trace

        root = Path(config.root_dir) if config.root_dir is not None else Path.cwd()
        bt = load_trace(artifacts[self.manifest_artifact], artifacts[self.vcd_artifact])
        events = json.loads(Path(artifacts[self.events_artifact]).read_text(encoding="utf-8"))

        out = {}
        render_timeline_full(bt, root / "results" / "timeline_full.svg")
        out["timeline_full_svg"] = root / "results" / "timeline_full.svg"
        render_timeline_job(bt, root / "results" / "timeline_job.svg", job=self.zoom_job)
        out["timeline_job_svg"] = root / "results" / "timeline_job.svg"
        render_firing_spans(events, root / "results" / "firing_spans.svg")
        out["firing_spans_svg"] = root / "results" / "firing_spans.svg"
        return out


@dataclass(kw_only=True)
class SyncDocsFiguresStep(BuildStep):
    """Promote the generated SVGs into the committed docs assets, on demand.

    Copies each manifest entry and writes a ``sync_status.json`` provenance record (per figure:
    source path, content hash) next to the committed assets — the cheap staleness signal a docs lint
    can check without re-running Vitis.  Mirrors the shared_mem / regmap workflow.
    """

    description: str = "Copy the timing figures into docs/images and record provenance."
    params: ClassVar[dict] = {}

    @property
    def consumes(self) -> list:  # type: ignore[override]
        return [f"{e['name']}_svg" for e in FIGURE_MANIFEST]

    @property
    def produces(self) -> dict:  # type: ignore[override]
        return {"docs_figures_sync": Path("docs/examples/memcpy/images/sync_status.json")}

    def run(self, config: BuildConfig, **_) -> dict[str, Any]:
        # docs/ lives at the repo root; the example dir is examples/mem_copy.
        repo_root = Path(config.root_dir).parents[1]
        records = []
        for entry in FIGURE_MANIFEST:
            src = Path(config.root_dir) / entry["source"]
            dst = repo_root / entry["dest"]
            if not src.exists():
                raise RuntimeError(f"Manifest source missing: {src} "
                                   f"(run --through timing_figures first)")
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(src.read_bytes())
            records.append({"name": entry["name"], "source": entry["source"],
                            "dest": entry["dest"], "source_sha256": _sha256(src)})
        sync_path = repo_root / "docs" / "examples" / "memcpy" / "images" / "sync_status.json"
        sync_path.parent.mkdir(parents=True, exist_ok=True)
        sync_path.write_text(json.dumps({"figures": records}, indent=2) + "\n", encoding="utf-8")
        return {"docs_figures_sync": sync_path}
