"""tests/examples/test_mem_copy_figures.py -- the committed timing figures are actually there.

Rendering needs a traced RTL run, so it is not tested here.  What *is* worth a cheap gate is that
every figure the docs page embeds exists as a committed asset: a missing SVG is a broken image on
docs/examples/memcpy/timing.md, and nothing else would catch it until someone opened the page.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


def _manifest():
    import sys
    sys.path.insert(0, str(REPO / "examples" / "mem_copy"))
    from mem_copy_figures import FIGURE_MANIFEST
    return FIGURE_MANIFEST


def test_every_manifest_figure_is_committed():
    for entry in _manifest():
        dst = REPO / entry["dest"]
        assert dst.exists(), (
            f"{entry['name']}: {entry['dest']} is missing. Regenerate with "
            f"`python examples/mem_copy/mem_copy_build.py --through sync_docs_figures`.")
        assert dst.stat().st_size > 0


def test_sync_status_records_every_figure():
    """The provenance record is the staleness signal a docs lint would read; it has to be complete.

    Not committed -- the repo's global ``*.json`` ignore covers it, matching how shared_mem and
    regmap do it (they commit the SVGs only) -- so this checks it when a local sync has produced
    one and skips otherwise."""
    path = REPO / "docs/examples/memcpy/images/sync_status.json"
    if not path.exists():
        pytest.skip("no local sync_status.json (it is a build output, not a committed asset)")
    status = json.loads(path.read_text(encoding="utf-8"))
    assert {f["name"] for f in status["figures"]} == {e["name"] for e in _manifest()}
    for f in status["figures"]:
        assert len(f["source_sha256"]) == 64


def test_timing_page_embeds_them():
    """A committed asset nothing references is dead weight; a reference with no asset is a broken
    image.  Pin both directions."""
    page = (REPO / "docs/examples/memcpy/timing.md").read_text(encoding="utf-8")
    for entry in _manifest():
        name = Path(entry["dest"]).name
        assert f"./images/{name}" in page, f"timing.md does not embed {name}"
