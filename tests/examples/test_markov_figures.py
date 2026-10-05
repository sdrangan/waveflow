"""The Markov docs figure: committed, recorded, embedded -- and exactly what the build renders.

The figure comes from the golden model (no toolchain), so unlike a trace-driven figure it can be
re-rendered here and compared byte for byte with the committed SVG: a docs figure that drifted from
the code fails, rather than going quietly stale.
"""
from __future__ import annotations

import json
from pathlib import Path

from examples.markov.markov_figures import FIGURE_MANIFEST, render_chain_output

REPO = Path(__file__).resolve().parents[2]


def test_every_manifest_figure_is_committed_and_recorded():
    status = json.loads((REPO / "docs/examples/markov/images/sync_status.json").read_text())
    assert {f["name"] for f in status["figures"]} == {e["name"] for e in FIGURE_MANIFEST}
    for e in FIGURE_MANIFEST:
        assert (REPO / e["dest"]).stat().st_size > 0


def test_the_committed_figure_is_what_the_build_renders(tmp_path):
    out = tmp_path / "chain_output.svg"
    render_chain_output(out)
    committed = REPO / "docs/examples/markov/images/chain_output.svg"
    assert out.read_bytes() == committed.read_bytes(), (
        "docs/examples/markov/images/chain_output.svg is stale: "
        "python -m examples.markov.markov_build --figures")


def test_the_pages_embed_it():
    for page in ("theory.md", "pysim.md"):
        text = (REPO / "docs/examples/markov" / page).read_text(encoding="utf-8")
        assert "images/chain_output.svg" in text, f"{page} does not embed the chain-output figure"
