"""The corpus gates: the D5 example list, the cards, and the build budget.

``test_every_toc_page_names_a_real_example_dir`` is the one the plan calls for
by name.  It exists because ``example_dir:`` is the *only* thing connecting a
docs page to the source it documents, two of the fourteen have a directory
name that differs from the page name, and a rename that misses the front
matter would silently drop an example out of the corpus with no other symptom.
"""
from __future__ import annotations

import time

import pytest

from waveflow.mcp.knowledge import (
    get_index,
    waveflow_browse,
    waveflow_get_doc,
    waveflow_get_example,
    waveflow_list_examples,
)
from waveflow.mcp.knowledge.corpus import TOC_DIR, parse_front_matter
from waveflow.mcp.knowledge.roots import repo_root

#: The docs TOC as of 2026-10-02 (mm_fir added; mmqueue -- the retiring VMAC example -- removed).  Pinned so that *adding* an example is a
#: deliberate two-line change rather than something that happens by accident,
#: and so the count in the plan stays honest.
TOC_EXAMPLES = {
    "basic_vec",
    "bram_access",
    "firblock",
    "interleaver",
    "memcpy",
    "mm_fir",
    "regmap",
    "rf_loopback",
    "rf_shot_loopback",
    "rf_shot_rx",
    "rf_shot_tx",
    "shared_mem",
    "stream_inband",
    "vecmult",
}

#: The two whose directory is not their page name -- the reason the key exists.
RENAMED = {
    "memcpy": "examples/mem_copy",
    "firblock": "examples/fir_block",
}


@pytest.fixture(scope="module")
def index():
    return get_index()


# ---------------------------------------------------------------------------
# D5: the example list
# ---------------------------------------------------------------------------


def test_every_toc_page_names_a_real_example_dir() -> None:
    """Each ``docs/examples/*/index.md`` has an ``example_dir:`` that exists."""
    root = repo_root()
    missing: list[str] = []
    dangling: list[str] = []
    for d in sorted(p for p in (root / TOC_DIR).iterdir() if p.is_dir()):
        index_md = d / "index.md"
        if not index_md.is_file():
            continue
        fm, _ = parse_front_matter(index_md.read_text(encoding="utf-8"))
        example_dir = fm.get("example_dir", "").strip()
        if not example_dir:
            missing.append(f"{TOC_DIR}/{d.name}/index.md")
        elif not (root / example_dir).is_dir():
            dangling.append(f"{TOC_DIR}/{d.name}/index.md -> {example_dir}")
    assert not missing, "TOC pages with no example_dir:\n  " + "\n  ".join(missing)
    assert not dangling, "example_dir names no such directory:\n  " + "\n  ".join(
        dangling
    )


def test_the_corpus_examples_are_exactly_the_toc(index) -> None:
    assert set(index.cards) == TOC_EXAMPLES


def test_renamed_examples_resolve_to_their_real_directory(index) -> None:
    for name, expected in RENAMED.items():
        assert index.cards[name].example_dir == expected


def test_non_toc_example_directories_are_out_of_the_corpus(index) -> None:
    """A directory under ``examples/`` with no TOC page is not offered at all.

    The point of D5: an example an agent is shown is one it may copy, and the
    older directories here are not that.
    """
    listed = {c.example_dir for c in index.cards.values()}
    excluded = {"examples/toy", "examples/bram_toy", "examples/state_toy",
                "examples/vecunit", "examples/dse_fir", "examples/block_scale"}
    assert not (listed & excluded)

    for name in ("toy", "bram_toy", "vecunit", "dse_fir"):
        result = waveflow_get_example(name)
        assert "error" in result

    indexed = {c.path.split("/")[1] for c in index.chunks if c.path.startswith("examples/")}
    # vmac: AXIMMQueue's example, retired from the docs (2026-10-02) so it is not offered as a pattern.
    for stray in ("toy", "bram_toy", "state_toy", "vecunit", "dse_fir", "_archive", "vmac"):
        assert stray not in indexed, f"examples/{stray} leaked into the index"


def test_files_linked_from_a_toc_page_come_along(index) -> None:
    """Files a TOC page points at outside its own directory are in the corpus.

    Derived from the pages, not hand-listed, so this asserts the derivation
    still finds the one that exists today.
    """
    paths = {c.path for c in index.chunks}
    assert any(p.startswith("examples/schemas/fixedpoint/") for p in paths), (
        "basic_vec names examples/schemas/fixedpoint as its counterpart"
    )


# ---------------------------------------------------------------------------
# The cards
# ---------------------------------------------------------------------------


def test_every_card_has_a_synopsis(index) -> None:
    thin = {
        name: card.synopsis
        for name, card in index.cards.items()
        if len(card.synopsis) < 40
    }
    assert not thin, f"cards with no usable synopsis: {thin}"


def test_card_synopsis_is_the_toc_summary(index) -> None:
    """Derived, not hand-labeled: the synopsis *is* the page's ``summary:``."""
    for name, card in index.cards.items():
        page = index.corpus.pages[card.doc_path]
        assert card.synopsis == page.summary


def test_list_examples_returns_every_card() -> None:
    listed = waveflow_list_examples()["examples"]
    assert {e["name"] for e in listed} == TOC_EXAMPLES
    for entry in listed:
        assert entry["synopsis"] and entry["example_dir"]
        assert "n_files" in entry and entry["n_files"] > 0


def test_get_example_returns_whole_files() -> None:
    card = waveflow_get_example("stream_inband")["example"]
    assert any(f["path"].endswith("poly.py") for f in card["files"])

    whole = waveflow_get_example("stream_inband", file="poly.py")
    assert whole["path"] == "examples/stream_inband/poly.py"
    assert whole["content"].count("\n") > 100
    assert "class PolyAccel" in whole["content"]


def test_get_example_rejects_a_file_from_another_example() -> None:
    result = waveflow_get_example("stream_inband", file="vecmult.py")
    assert "error" in result


# ---------------------------------------------------------------------------
# Browse and get_doc
# ---------------------------------------------------------------------------


def test_browse_top_level_then_a_section() -> None:
    top = waveflow_browse()
    assert {s["section"] for s in top["sections"]} == {"guide", "examples"}

    hooks = waveflow_browse("guide/custom_hooks")
    assert hooks["pages"]
    assert all(p["title"] for p in hooks["pages"])

    build = waveflow_browse("guide/build")
    assert build["pages"], "docs/guide/build is a guide section, not build output"


def test_browse_examples_lists_the_cards() -> None:
    assert {e["name"] for e in waveflow_browse("examples")["examples"]} == TOC_EXAMPLES


def test_get_doc_whole_page_and_one_heading() -> None:
    page = waveflow_get_doc("docs/guide/custom_hooks/writing.md")
    assert page["title"] and page["content"].startswith("---")

    headings = [c.heading for c in get_index().chunks
                if c.path == "docs/guide/custom_hooks/writing.md"]
    section = waveflow_get_doc("docs/guide/custom_hooks/writing.md", heading=headings[-1])
    assert section["content"] and len(section["content"]) < len(page["content"])


def test_get_doc_miss_suggests_pages() -> None:
    result = waveflow_get_doc("docs/guide/no_such_page.md")
    assert "error" in result and "did_you_mean" in result


# ---------------------------------------------------------------------------
# The build budget
# ---------------------------------------------------------------------------


def test_index_builds_in_under_three_seconds() -> None:
    """The whole index, from a warm page cache, inside the plan's 3 s budget.

    Measured on a rebuild rather than on ``get_index()``, which is cached for
    the process: the number that matters is what an MCP server pays at start.
    A cold first read of 5 MB costs several seconds more and is not what this
    bounds -- the budget is for the work, not for the disk.
    """
    get_index()  # warm the OS page cache; the first read is not the subject
    start = time.perf_counter()
    rebuilt = get_index(refresh=True)
    elapsed = time.perf_counter() - start
    assert elapsed < 3.0, f"index build took {elapsed:.2f}s"
    assert rebuilt.chunks and rebuilt.cards
