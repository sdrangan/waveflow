"""Committed-figure workflow for ``rf_shot_tx``'s docs pages.

One figure, and it shows the thing prose cannot: **what actually comes out of the converter**, for
both command streams, on one sample axis.

* **playout.svg** — the finite scenario above, the infinite one below.  The finite path plays three
  whole passes and then *goes quiet*; the infinite one plays waveform A, hands the memory over, and
  comes back with waveform B.  The filler between and around them is drawn as filler rather than as
  absence, because it is a **value** the design writes — a converter comes due on a grid and a design
  that stopped writing would starve it.

**Why this is the figure worth having.** ``tx.md`` already carries a Mermaid architecture diagram,
and Mermaid does not rot.  What no diagram in this family shows is the *playout shape* — and that
shape is the whole design: the finite path's trailing quiet is what ``SHOT_BUSY`` protects, and the
loop path's gap is what a single-region handover costs.

**The source is pysim, not a VCD, and that is deliberate.**
``plans/rf_shot_unify.md`` Stage C declined to add a figure it could not gate, having watched
``rf_interfaces.svg`` go stale for a whole arc.  Two things answer that objection here:

* **It regenerates with no toolchain.**  A VCD-sourced figure needs Vivado to re-render, so in
  practice it is re-rendered rarely and rots quietly.  This one is a plain ``python
  rf_shot_tx_build.py --through sync_docs_figures`` on any machine.
* **Its equality with the RTL is already a gate.**
  ``tests/examples/test_rf_shot_tx_xsi.py::test_both_backends_agree_sample_for_sample`` asserts the
  pysim playout is byte-identical to the RTL one over the common horizon.  So a figure drawn from
  pysim is a figure of the RTL, and something already fails if that stops being true.

Generated SVGs land in ``results/`` (gitignored); :class:`SyncDocsFiguresStep` promotes them into
``docs/examples/rf_shot_tx/images/`` as committed assets and writes ``sync_status.json`` beside them
— source path plus content hash, the cheap staleness signal a docs lint can check.  Same shape as
``bram_access``'s and ``mem_copy``'s.

Run it with::

    python rf_shot_tx_build.py --through sync_docs_figures
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
matplotlib.rcParams["svg.hashsalt"] = "rf_shot_tx_figures"
import matplotlib.pyplot as plt  # noqa: E402

from waveflow.build.build import BuildConfig, BuildStep  # noqa: E402

FIGURE_MANIFEST = [
    {"name": "playout", "source": "results/playout.svg",
     "dest": "docs/examples/rf_shot_tx/images/playout.svg"},
]

#: Warm for the samples the host asked for, cool-grey for the filler the design writes when it has
#: nothing to play.  One mapping across both panels, so a reader carries it down the page.
C_SAMP = "#c44e52"
C_FILL = "#9aa5b1"


def _sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _playouts() -> dict[str, np.ndarray]:
    """Run both scenarios in pysim and return each one's converter-code sequence."""
    from examples.rf_shot_tx.rf_shot_tx import (
        FINITE_FRAMES,
        LOOP_FRAMES,
        played_samples,
        run_pysim,
    )

    return {name: played_samples(run_pysim(frames=frames, in_bundle=f"vectors/{name}"))
            for name, frames in (("cmd", FINITE_FRAMES), ("cmd_loop", LOOP_FRAMES))}


def render(played: dict[str, np.ndarray], out: Path) -> Path:
    """Draw both playouts on one sample axis and write *out*."""
    from examples.rf_shot_tx.rf_shot_tx import BLKSIZE, segments

    titles = {
        "cmd": "SHOT_LOAD — three passes, then quiet",
        "cmd_loop": "SHOT_LOOP — waveform A, a handover, waveform B",
    }
    fig, axes = plt.subplots(2, 1, figsize=(9.5, 5.0), sharex=True)
    for ax, name in zip(axes, ("cmd", "cmd_loop"), strict=True):
        y = np.asarray(played[name], dtype=float)
        x = np.arange(y.size)
        ax.plot(x, y, lw=0.8, color=C_SAMP)
        # Shade every filler run, so "quiet" reads as something the design DID rather than a gap.
        pos = 0
        for is_filler, run in segments(played[name]):
            if is_filler:
                ax.axvspan(pos, pos + run.size, color=C_FILL, alpha=0.28, lw=0)
            pos += run.size
        for b in range(0, int(y.size) + 1, BLKSIZE):
            ax.axvline(b, color="#d0d0d0", lw=0.4, zorder=0)
        ax.set_title(titles[name], fontsize=10, loc="left")
        ax.set_ylabel("converter code")
        ax.margins(x=0)
    axes[-1].set_xlabel(f"sample index  (gridlines every {BLKSIZE}-sample converter block)")
    fig.suptitle("rf_shot_tx playout — shaded runs are FILLER the design writes, not silence",
                 fontsize=11)
    fig.tight_layout()
    out.parent.mkdir(parents=True, exist_ok=True)
    # metadata={"Date": None} is what makes the SVG DETERMINISTIC: matplotlib otherwise stamps a
    # <dc:date> and every re-render diffs for no reason -- which is how a committed figure becomes
    # noise a reviewer learns to ignore.  Same as bram_access's.
    fig.savefig(out, format="svg", bbox_inches="tight", metadata={"Date": None})
    plt.close(fig)
    return out


@dataclass(kw_only=True)
class PlayoutFigureStep(BuildStep):
    """Render ``results/playout.svg`` from a pysim run of both scenarios."""

    description: str = "Render the two playouts as one committed-figure SVG."
    consumes: ClassVar[list] = ["rf_shot_tx_source"]
    produces: ClassVar[dict] = {"playout_svg": Path("results/playout.svg")}
    params: ClassVar[dict] = {}

    def run(self, config: BuildConfig, **_) -> dict[str, Any]:
        out = Path(config.root_dir) / "results" / "playout.svg"
        return {"playout_svg": render(_playouts(), out)}


@dataclass(kw_only=True)
class SyncDocsFiguresStep(BuildStep):
    """Promote the generated SVGs into the committed docs assets, on demand.

    Copies each manifest entry and writes a ``sync_status.json`` provenance record (per figure:
    source path, content hash) beside the committed assets — the cheap staleness signal a docs lint
    can check without re-rendering.  Mirrors the ``bram_access`` / ``mem_copy`` workflow.
    """

    description: str = "Copy the playout figure into docs/images and record provenance."
    params: ClassVar[dict] = {}

    @property
    def consumes(self) -> list:  # type: ignore[override]
        return [f"{e['name']}_svg" for e in FIGURE_MANIFEST]

    @property
    def produces(self) -> dict:  # type: ignore[override]
        return {"docs_figures_sync": Path("docs/examples/rf_shot_tx/images/sync_status.json")}

    def run(self, config: BuildConfig, **_) -> dict[str, Any]:
        repo_root = Path(config.root_dir).parents[1]
        records = []
        for entry in FIGURE_MANIFEST:
            src = Path(config.root_dir) / entry["source"]
            dst = repo_root / entry["dest"]
            if not src.exists():
                raise RuntimeError(f"Manifest source missing: {src} "
                                   f"(run --through playout_figure first)")
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(src.read_bytes())
            records.append({"name": entry["name"], "source": entry["source"],
                            "dest": entry["dest"], "source_sha256": _sha256(src)})
        sync_path = repo_root / "docs" / "examples" / "rf_shot_tx" / "images" / "sync_status.json"
        sync_path.parent.mkdir(parents=True, exist_ok=True)
        sync_path.write_text(json.dumps({"figures": records}, indent=2) + "\n", encoding="utf-8")
        return {"docs_figures_sync": sync_path}
