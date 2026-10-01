"""Paraphrase queries: the ones that decide whether Stage 1b is needed.

These are the six from the 2026-09-29 probe, phrased the way someone who has
never read the Waveflow docs would phrase them -- deliberately avoiding the
words the docs use.  BM25 found 2 of 6.

They are ``xfail(strict=False)`` on purpose, and that is the whole point of
the file: the plan does **not** require them to pass in Stage 1.  Each one
records the page it should reach and **how** it is meant to be reached:

* ``BM25`` -- :func:`waveflow_search` should find it.  An ``XPASS`` here is
  good news; BM25 got better.
* ``BROWSE`` -- the model is expected to get there by reading titles and
  summaries through :func:`waveflow_browse`.  That path cannot be measured
  without an agent driving, so the search assertion is expected to fail and
  the case is here as a record, not a gate.  What *is* asserted for these is
  the precondition browsing depends on: the page is reachable in the tree and
  carries a summary saying what it is about.

Stage 1b (a local ONNX embedding ranker fused with BM25) is justified only if
the ``BROWSE`` rows still miss once an agent is driving, in Stage 0 or 6.  So
the file is evidence, not a pass/fail gate, and it is kept apart from
``test_retrieval_eval.py`` for that reason.
"""
from __future__ import annotations

import pytest

from waveflow.mcp.knowledge import get_index, waveflow_search

BM25 = "BM25"
BROWSE = "browse"

#: ``(query, expected page prefix, the path it is reached by)``.
#:
#: The path column is **measured, not aspired to**: as of 2026-09-29 this
#: implementation reaches 1 of the 6 by BM25, where the plan's 60-line probe
#: reached 2.  Which one the probe also got is not recorded, and the
#: guide-over-source prior in `waveflow_search` is not the difference -- with
#: the prior forced to 1.0 the result is still 1 of 6.  Either way the
#: conclusion the plan drew stands and is if anything firmer: BM25 alone does
#: not answer a query that avoids Waveflow's vocabulary.
PARAPHRASES: list[tuple[str, str, str]] = [
    (
        "how do I tell it to stop",
        "docs/examples/stream_inband/",
        BROWSE,
    ),
    (
        "where do I put my own C++ for the math part",
        "docs/guide/custom_hooks/",
        BM25,
    ),
    (
        "how do I check how many clock ticks it took",
        "docs/guide/timing/",
        BROWSE,
    ),
    (
        "how does the CPU set the configuration values",
        "docs/guide/interface/primitive/regmap.md",
        BROWSE,
    ),
    (
        "my packet ends too soon and it breaks",
        "docs/guide/custom_hooks/stream.md",
        BROWSE,
    ),
    (
        "store some numbers then multiply them",
        "docs/examples/vecmult/",
        BROWSE,
    ),
    # The two the rotate blind-test agent was stuck on (PR #209).  Measured by
    # BM25 on 2026-10-01, after the "What a SeqTB body can express" section:
    # the loop question's top hit is that section; the TLAST one reaches the
    # page at rank 2.
    (
        "how do I loop over transactions in the testbench",
        "docs/guide/comp_codegen/testbench.md",
        BM25,
    ),
    (
        "how do I send a burst without TLAST",
        "docs/guide/comp_codegen/testbench.md",
        BM25,
    ),
]

_IDS = [q for q, _, _ in PARAPHRASES]


@pytest.mark.parametrize("query,expected,path", PARAPHRASES, ids=_IDS)
@pytest.mark.xfail(
    strict=False,
    reason="newcomer phrasing; not a Stage 1 gate -- see the module docstring",
)
def test_paraphrase_reaches_page_by_search(query: str, expected: str, path: str) -> None:
    hits = waveflow_search(query, k=5)["hits"]
    assert any(h["path"].startswith(expected) for h in hits), (
        f"{query!r} ({path}) reached nothing under {expected!r}.\n"
        + "\n".join(f"  {h['path']}  [{h['score']}]  {h['heading']}" for h in hits)
    )


@pytest.mark.parametrize("query,expected,path", PARAPHRASES, ids=_IDS)
def test_paraphrase_target_is_reachable_by_browsing(
    query: str, expected: str, path: str
) -> None:
    """The precondition for the ``browse`` path: the page exists and says what it is.

    Not a restatement of the card test.  This asserts the specific pages the
    paraphrase probe depends on are in the tree with a non-trivial summary --
    if one of them loses its ``summary:``, the browse path silently degrades
    and only the agent runs in Stage 0/6 would notice.
    """
    index = get_index()
    pages = [p for p in index.corpus.pages if p.startswith(expected)]
    assert pages, f"no page under {expected!r} for {query!r}"
    summarised = [p for p in pages if len(index.corpus.pages[p].summary) > 40]
    assert summarised, (
        f"every page under {expected!r} lacks a usable summary, so a model "
        f"browsing the tree cannot match {query!r} by meaning"
    )
