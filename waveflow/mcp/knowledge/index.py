"""The knowledge index: one object holding the corpus, BM25 and the usage map.

Built once per process at server start, in memory, from the live checkout.  The
whole build is around a second warm and a few seconds cold, so there is nothing
to persist and nothing to invalidate.

The BM25 field weights encode the plan's split between the two ways of finding
things.  Browsing hands the *model* every page's ``title`` + ``summary`` and
lets it do the meaning-matching; BM25 is for the queries that use Waveflow's
own words.  Weighting title, summary and heading above the body is what makes
the exact-words path land on the page whose subject a term is, rather than the
longest page that happens to mention it.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from waveflow.mcp.knowledge.bm25 import Bm25Index
from waveflow.mcp.knowledge.corpus import Chunk, Corpus, load_corpus
from waveflow.mcp.knowledge.examples import ExampleCard, build_cards
from waveflow.mcp.knowledge.usage import UsageIndex, build_usage_index

__all__ = ["KnowledgeIndex", "get_index"]

#: How much a chunk's title, summary and heading count for, relative to a line
#: of its body.  The heading outweighs the title because a query naming a
#: section ("persistent loop END command") should beat the page that merely
#: carries the words somewhere.
_W_TITLE = 3
_W_SUMMARY = 3
_W_HEADING = 5
_W_BODY = 1
#: A path is a real signal in this corpus: `docs/guide/custom_hooks/...` tells
#: a "hand-written compute hook" query most of what it needs.
_W_PATH = 2


@dataclass
class KnowledgeIndex:
    """Everything the six knowledge tools read."""

    corpus: Corpus
    usage: UsageIndex
    cards: dict[str, ExampleCard]
    bm25: Bm25Index
    build_seconds: float = 0.0
    #: parallel to the BM25 document ids
    chunks: list[Chunk] = field(default_factory=list)

    @property
    def root(self) -> Path:
        return self.corpus.root


def _build(root: Path | None, refresh: bool) -> KnowledgeIndex:
    t0 = time.perf_counter()
    corpus = load_corpus(root, refresh=refresh)
    usage = build_usage_index(corpus)
    cards = build_cards(corpus, usage)

    bm25 = Bm25Index()
    for chunk in corpus.chunks:
        bm25.add(
            [
                (chunk.path.replace("/", " ").replace("_", " "), _W_PATH),
                (chunk.title, _W_TITLE),
                (chunk.summary, _W_SUMMARY),
                (chunk.heading, _W_HEADING),
                (chunk.text, _W_BODY),
            ]
        )
    bm25.finalize()

    return KnowledgeIndex(
        corpus=corpus,
        usage=usage,
        cards=cards,
        bm25=bm25,
        chunks=list(corpus.chunks),
        build_seconds=time.perf_counter() - t0,
    )


_INDEX: dict[str, KnowledgeIndex] = {}


def get_index(root: Path | None = None, *, refresh: bool = False) -> KnowledgeIndex:
    """The process-wide index for *root*, built on first use."""
    key = str(Path(root).resolve()) if root is not None else ""
    if refresh:
        _INDEX.clear()
    if key not in _INDEX:
        _INDEX[key] = _build(root, refresh)
    return _INDEX[key]
