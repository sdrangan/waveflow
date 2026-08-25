"""Committed-figure workflow for ``bram_simple``'s timing pages.

Two figures, in the order ``docs/examples/bram_simple/timing.md`` tells the story:

* **activity_full.svg** — the whole run on one cycle axis.  Phase 1 is unmistakable: the writer's
  256-word fill with the read port completely idle, because the reader has not been armed yet.  Then
  the answers start, and the last two lanes overlap.
* **activity_overlap.svg** — the overlap window, beat by beat.  The write port and the read port are
  busy in the *same cycles*, on ranges the caller kept disjoint.  This is what a true-dual-port
  memory is for, and it is the picture that makes "no hazard is a convention, not a structure" a
  thing you can see rather than a claim.

**The lanes are the memory's own pins, not only the design's streams**, and that is the difference
from every other timing figure in this repo.  ``buf_w`` and ``buf_r`` are wires of the *wrapper* —
the join between the kernel's ``mode=bram`` ports and the hand-written ``bram_t2p`` — so a level-1
``$dumpvars`` of the elaborated top sees them, and the figure can show the memory being used rather
than inferring it from the streams at the boundary.

Generated SVGs land in ``results/`` (gitignored); :class:`SyncDocsFiguresStep` promotes them — on
demand, via an explicit manifest — into ``docs/examples/bram_simple/images/`` as committed assets, so
a docs figure only changes when you intend it to and the change is a reviewable ``git diff``.  Same
shape as ``mem_copy``'s and ``shared_mem``'s.

Run it with::

    python bram_simple_build.py --through sync_docs_figures

which needs a traced RTL run first (``--through rtl_trace``).
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
# Deterministic SVG: a stable hashsalt fixes the element ids matplotlib would otherwise randomize, so
# a re-render only diffs when the figure truly changed.
matplotlib.rcParams["svg.hashsalt"] = "bram_simple_figures"
import matplotlib.pyplot as plt  # noqa: E402

from waveflow.build.build import BuildConfig, BuildStep  # noqa: E402
from waveflow.utils.timing import ActivityDiagram  # noqa: E402

FIGURE_MANIFEST = [
    {"name": "activity_full", "source": "results/activity_full.svg",
     "dest": "docs/examples/bram_simple/images/activity_full.svg"},
    {"name": "activity_overlap", "source": "results/activity_overlap.svg",
     "dest": "docs/examples/bram_simple/images/activity_overlap.svg"},
]

#: One colour per lane, reused across both figures so a reader carries the mapping from the whole-run
#: view into the zoom.  The two memory lanes share a family (warm) and the two stream lanes another
#: (cool), because the interesting comparison is memory-vs-stream, not lane-vs-lane.
C_DATA_W, C_BUF_W, C_BUF_R, C_DATA_R = "#4C78A8", "#E45756", "#F58518", "#54A24B"


def _save_svg(fig, path: Path) -> None:
    """Write a deterministic SVG (no embedded timestamp)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, format="svg", bbox_inches="tight", metadata={"Date": None})
    plt.close(fig)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def lanes(vcd_path: Path, manifest: dict) -> list[tuple[str, np.ndarray, str]]:
    """The four lanes both figures draw, top to bottom: payload in, memory, payload out.

    ``buf_w`` fires on ``en && we`` and ``buf_r`` on ``en`` — which is exactly the memory's own
    ``always`` block, minus the address comparison that
    :func:`~waveflow.utils.bram_trace.find_read_during_write` adds.
    """
    from waveflow.utils.bram_trace import port_samples, sampled

    w = port_samples(vcd_path, manifest, "write")
    r = port_samples(vcd_path, manifest, "read")
    sig = sampled(vcd_path, manifest,
                  "data_w_TVALID", "data_w_TREADY", "data_r_TVALID", "data_r_TREADY")

    def fire(valid: str, ready: str) -> np.ndarray:
        return np.nonzero((sig[valid] != 0) & (sig[ready] != 0))[0]

    return [
        ("data_w  (payload in)", fire("data_w_TVALID", "data_w_TREADY"), C_DATA_W),
        ("buf_w   (memory write)", np.nonzero((w.en != 0) & (w.we != 0))[0], C_BUF_W),
        ("buf_r   (memory read)", np.nonzero(r.en != 0)[0], C_BUF_R),
        ("data_r  (payload out)", fire("data_r_TVALID", "data_r_TREADY"), C_DATA_R),
    ]


def overlap_window(lane_list, pad: int = 12) -> tuple[int, int]:
    """The cycles in which the write port and the read port are **both** busy, plus a margin.

    Derived from the run rather than written down, so the zoom cannot drift away from the thing it is
    meant to show.  A run with no overlap raises instead of rendering an arbitrary window — the whole
    point of the figure is that the overlap exists.
    """
    by = {label.split()[0]: ev for label, ev, _c in lane_list}
    lo_w, hi_w = int(by["buf_w"].min()), int(by["buf_w"].max())
    reads = by["buf_r"]
    both = reads[(reads >= lo_w) & (reads <= hi_w)]
    if not len(both):
        raise RuntimeError(
            "no cycle has the read port active inside the write port's span, so there is no overlap "
            "to zoom on. Either the scenario changed or the reader is no longer armed mid-write.")
    return max(int(both.min()) - pad, 0), int(both.max()) + pad


def render_activity_full(lane_list, path: Path) -> None:
    """The whole run, as bands."""
    ad = ActivityDiagram(lane_list)
    fig, ax, _ = ad.plot(
        mode="band", gap=3, fig_width=11, fig_height=3.0,
        title="bram_simple — one scenario-zero run (the write port fills first; nothing reads "
              "until the token arrives)")
    _save_svg(fig, path)


def render_activity_overlap(lane_list, path: Path) -> None:
    """The overlap, beat by beat."""
    lo, hi = overlap_window(lane_list)
    ad = ActivityDiagram(lane_list)
    fig, ax, _ = ad.plot(
        mode="beat", trange=(lo, hi), fig_width=11, fig_height=3.0,
        title=f"the overlap, cycles {lo}–{hi} — both memory ports busy in the same cycles, "
              f"on disjoint ranges")
    _save_svg(fig, path)


# ---------------------------------------------------------------------------
# Build steps
# ---------------------------------------------------------------------------

@dataclass(kw_only=True)
class ActivityFiguresStep(BuildStep):
    """Render both activity figures into ``results/`` from the traced run."""

    description: str = "Render the bram_simple activity figures from the traced RTL run."
    params: ClassVar[dict] = {}

    vcd_artifact: str = "trace_vcd"

    @property
    def consumes(self) -> list:  # type: ignore[override]
        return [self.vcd_artifact]

    @property
    def produces(self) -> dict:  # type: ignore[override]
        return {f"{e['name']}_svg": Path(e["source"]) for e in FIGURE_MANIFEST}

    def run(self, config: BuildConfig, **artifacts) -> dict[str, Any]:
        from examples.bram_simple.bram_simple_build import hazard_manifest

        root = Path(config.root_dir) if config.root_dir is not None else Path.cwd()
        lane_list = lanes(Path(artifacts[self.vcd_artifact]), hazard_manifest())
        out = {}
        for name, render in (("activity_full", render_activity_full),
                             ("activity_overlap", render_activity_overlap)):
            dst = root / "results" / f"{name}.svg"
            render(lane_list, dst)
            out[f"{name}_svg"] = dst
        return out


@dataclass(kw_only=True)
class SyncDocsFiguresStep(BuildStep):
    """Promote the generated SVGs into the committed docs assets, on demand.

    Copies each manifest entry and writes a ``sync_status.json`` provenance record (per figure:
    source path, content hash) beside the committed assets — the cheap staleness signal a docs lint
    can check without re-running Vivado.  Mirrors the ``mem_copy`` / ``shared_mem`` workflow.
    """

    description: str = "Copy the activity figures into docs/images and record provenance."
    params: ClassVar[dict] = {}

    @property
    def consumes(self) -> list:  # type: ignore[override]
        return [f"{e['name']}_svg" for e in FIGURE_MANIFEST]

    @property
    def produces(self) -> dict:  # type: ignore[override]
        return {"docs_figures_sync": Path("docs/examples/bram_simple/images/sync_status.json")}

    def run(self, config: BuildConfig, **_) -> dict[str, Any]:
        repo_root = Path(config.root_dir).parents[1]
        records = []
        for entry in FIGURE_MANIFEST:
            src = Path(config.root_dir) / entry["source"]
            dst = repo_root / entry["dest"]
            if not src.exists():
                raise RuntimeError(f"Manifest source missing: {src} "
                                   f"(run --through activity_figures first)")
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(src.read_bytes())
            records.append({"name": entry["name"], "source": entry["source"],
                            "dest": entry["dest"], "source_sha256": _sha256(src)})
        sync_path = repo_root / "docs" / "examples" / "bram_simple" / "images" / "sync_status.json"
        sync_path.parent.mkdir(parents=True, exist_ok=True)
        sync_path.write_text(json.dumps({"figures": records}, indent=2) + "\n", encoding="utf-8")
        return {"docs_figures_sync": sync_path}
