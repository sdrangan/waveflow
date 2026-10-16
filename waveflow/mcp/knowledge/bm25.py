"""A small in-house BM25 with a code-aware tokenizer.

Why in-house: the ranking has to be *inspectable*, because a pytest eval pins
it (``tests/mcp/test_retrieval_eval.py``).  When a query stops finding its page
the fix has to be a readable change to a scoring rule, not a dependency bump.
There is no index to persist either -- the whole corpus scores in about a
millisecond -- so a library would buy nothing.

The tokenizer is the part that matters for this corpus.  A plain word splitter
loses either the identifier or its parts: split ``read_axi4_stream_lane`` and a
search for the exact symbol ranks every page mentioning ``stream``; keep it
whole and a search for ``axi4 stream lane`` misses it.  :func:`tokenize`
therefore emits **both** -- the identifier as one token *and* its ``snake_case``
/ ``CamelCase`` pieces.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from functools import lru_cache

__all__ = ["tokenize", "Bm25Index"]

#: Identifiers (including ``a_b``/``A::B`` pieces, split below) and bare numbers.
_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|\d+")

#: ``CamelCase`` boundaries, including the ``HLSStream`` -> ``HLS`` + ``Stream``
#: acronym case that a naive ``(?<=[a-z])(?=[A-Z])`` split gets wrong.
_CAMEL = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z0-9]*|[a-z0-9]+")

#: Dropped from queries and documents alike.  Deliberately short: this corpus is
#: technical prose where most "common" words (``stream``, ``data``, ``read``)
#: carry real signal, and BM25's IDF already discounts whatever is everywhere.
_STOP = frozenset(
    """a an and are as at be by for from has have how i if in into is it its of on
    or that the their then there these this to was were what when where which with
    you your""".split()
)


def _split_identifier(word: str) -> list[str]:
    """The pieces of ``word``, or ``[]`` when it has none worth adding."""
    parts = [p for p in word.split("_") if p]
    out: list[str] = []
    for p in parts:
        pieces = _CAMEL.findall(p)
        out.extend(pieces if pieces else [p])
    out = [p.lower() for p in out if len(p) > 1]
    if len(out) <= 1 and (not out or out[0] == word.lower()):
        return []
    return out


@lru_cache(maxsize=1 << 16)
def _expand(word: str) -> tuple[str, ...]:
    """Every token *word* contributes: itself, then its pieces.

    Cached because this corpus is code: a few thousand distinct identifiers
    account for millions of occurrences, and the split is most of the cost of
    building the index.
    """
    low = word.lower()
    if low in _STOP:
        return ()
    return (low, *(t for t in _split_identifier(word) if t not in _STOP))


def tokenize(text: str) -> list[str]:
    """Tokens for *text*: whole identifiers plus their pieces, lowercased."""
    out: list[str] = []
    for m in _WORD.finditer(text):
        out.extend(_expand(m.group(0)))
    return out


@dataclass
class Bm25Index:
    """BM25 over a fixed list of documents, each a bag of tokens.

    Documents are added as *weighted* token groups so a chunk's heading and its
    page title count for more than a line in its body: :meth:`add` takes a list
    of ``(text, weight)`` pairs and repeats each group's tokens ``weight``
    times in the term-frequency counts.  That is the standard cheap stand-in
    for a multi-field BM25F, and it keeps the scorer a single loop.
    """

    k1: float = 1.2
    b: float = 0.75

    _tf: list[Counter[str]] = field(default_factory=list)
    _len: list[int] = field(default_factory=list)
    _df: Counter[str] = field(default_factory=Counter)
    _avglen: float = 0.0
    _idf: dict[str, float] = field(default_factory=dict)
    _postings: dict[str, list[int]] = field(default_factory=dict)

    def add(self, fields: list[tuple[str, int]]) -> int:
        """Add one document; return its index."""
        tf: Counter[str] = Counter()
        for text, weight in fields:
            if not text or weight <= 0:
                continue
            toks = tokenize(text)
            if weight == 1:
                tf.update(toks)
            else:
                for t in toks:
                    tf[t] += weight
        self._tf.append(tf)
        self._len.append(sum(tf.values()))
        self._df.update(tf.keys())
        return len(self._tf) - 1

    def finalize(self) -> None:
        """Precompute IDF and the postings lists.  Call once, after all adds."""
        n = len(self._tf)
        self._avglen = (sum(self._len) / n) if n else 0.0
        self._idf = {
            term: math.log(1.0 + (n - df + 0.5) / (df + 0.5))
            for term, df in self._df.items()
        }
        postings: dict[str, list[int]] = {}
        for i, tf in enumerate(self._tf):
            for term in tf:
                postings.setdefault(term, []).append(i)
        self._postings = postings

    def search(self, query: str, k: int = 8) -> list[tuple[int, float]]:
        """Top-*k* ``(document index, score)``, best first."""
        terms = tokenize(query)
        if not terms or not self._tf:
            return []
        scores: dict[int, float] = {}
        avg = self._avglen or 1.0
        # Query-side term frequency: a term the user repeated counts twice, and
        # `tokenize` repeats the pieces of every identifier, which is what makes
        # an exact-symbol query outrank a page that only shares one piece.
        for term, qtf in Counter(terms).items():
            idf = self._idf.get(term)
            if idf is None:
                continue
            for i in self._postings[term]:
                tf = self._tf[i][term]
                norm = tf * (self.k1 + 1.0) / (
                    tf + self.k1 * (1.0 - self.b + self.b * self._len[i] / avg)
                )
                scores[i] = scores.get(i, 0.0) + idf * norm * qtf
        ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
        return ranked[:k]

    def __len__(self) -> int:
        return len(self._tf)
