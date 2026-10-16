"""AC13 of ``plans/cpu_model.md``: the processor model's pages exist, with front matter.

The pages' runnable blocks are checked by ``tests/docs/test_doc_snippets.py`` (they opt in with
``snippets: run``) and their quoted numbers by ``tests/docs/test_documented_numbers.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

DOCS = Path(__file__).resolve().parents[2] / "docs"
PAGES = {
    "guide/cpu/index.md": ("Processor Model", "Guide"),
    "guide/cpu/scheduling.md": ("Scheduling", "Processor Model"),
    "guide/cpu/calibration.md": ("Calibration", "Processor Model"),
    "guide/cpu/dse.md": ("Using it in a DSE", "Processor Model"),
    "examples/cpu_sched/index.md": ("Micro-scheduler on a CPU", "Examples"),
}


@pytest.mark.parametrize("rel", sorted(PAGES))
def test_the_page_exists_with_its_front_matter(rel):
    text = (DOCS / rel).read_text(encoding="utf-8")
    assert text.startswith("---\n")
    front = text.split("---\n")[1]
    title, parent = PAGES[rel]
    assert f"title: {title}\n" in front and f"parent: {parent}\n" in front
    assert "summary: " in front


def test_the_runnable_pages_opt_in():
    for rel in ("guide/cpu/scheduling.md", "guide/cpu/dse.md"):
        assert (
            "snippets: run"
            in (DOCS / rel).read_text(encoding="utf-8").split("---\n")[1]
        )
