"""The six knowledge tools, as plain functions.

CLI first, MCP as a thin wrapper: each of these is an ordinary Python function
returning JSON-able data, reachable from a terminal through ``waveflow kb`` and
registered with FastMCP by one line in ``registry.py``.  An agent with no MCP
support at all can still use every one of them.

**Find in chunks, read whole.**  :func:`waveflow_search` returns pointers --
path, heading, line range, a three-line snippet.  :func:`waveflow_get_doc` and
:func:`waveflow_get_example` return whole files.  Nothing here hands back a
reassembled half-file, because a half-file is how an agent ends up inventing
the other half.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from waveflow.mcp.knowledge.corpus import generated_note
from waveflow.mcp.knowledge.index import get_index

__all__ = [
    "waveflow_browse",
    "waveflow_search",
    "waveflow_find_usage",
    "waveflow_list_examples",
    "waveflow_get_example",
    "waveflow_get_doc",
]

#: Sections from one file allowed in a single result set.  See `waveflow_search`.
_MAX_PER_PATH = 2

#: Score multipliers by where a chunk comes from.
#:
#: BM25 alone ranks by word overlap, and on this corpus that systematically
#: favours the wrong kind of hit: a query like "BuildDag build steps" matches
#: six example ``*_build.py`` files -- each literally defining a
#: ``build_<name>_dag`` -- ahead of ``docs/guide/build/``, the page written to
#: answer it.  The guide is the reference, the example docs are walkthroughs of
#: one design, and the sources are evidence; `waveflow_find_usage` already
#: exists for "show me this symbol in use", so search does not need to lead
#: with source files.  These are deliberately mild: a source file that is
#: clearly the best match still wins.
_PRIOR_GUIDE = 1.15
_PRIOR_EXAMPLE_DOC = 1.0
_PRIOR_SOURCE = 0.8


def _prior(path: str, kind: str) -> float:
    if path.startswith("docs/guide/"):
        return _PRIOR_GUIDE
    if kind == "doc":
        return _PRIOR_EXAMPLE_DOC
    if path.endswith(".md"):
        return _PRIOR_EXAMPLE_DOC
    return _PRIOR_SOURCE


def _idx(root: str | None = None):
    return get_index(Path(root) if root else None)


# ---------------------------------------------------------------------------
# browse
# ---------------------------------------------------------------------------


def waveflow_browse(section: str | None = None, root: str | None = None) -> dict[str, Any]:
    """The doc tree under *section*, with every page's title and summary.

    This is the semantic half of retrieval: the summaries were written for
    humans reading the docs index, and handing them to the model lets *it* do
    the meaning-matching that BM25 cannot.  ``section="examples"`` also lists
    the example cards.
    """
    index = _idx(root)
    sec = (section or "").strip().strip("/")

    if not sec:
        return {
            "sections": [
                {
                    "section": "guide",
                    "path": "docs/guide",
                    "title": "Guide",
                    "summary": "How to use Waveflow: schemas, modules, "
                    "interfaces, simulation, codegen, build, timing, RF.",
                    "n_pages": sum(
                        1 for p in index.corpus.pages if p.startswith("docs/guide/")
                    ),
                },
                {
                    "section": "examples",
                    "path": "docs/examples",
                    "title": "Examples",
                    "summary": "The worked reference designs, each with its "
                    "source directory.",
                    "n_pages": len(index.cards),
                },
            ],
            "hint": "browse('guide') or browse('examples'); "
            "browse('guide/custom_hooks') for a subsection.",
        }

    if sec in ("examples", "docs/examples"):
        return {
            "section": "examples",
            "examples": [
                c.to_dict(with_files=False)
                for c in sorted(index.cards.values(), key=lambda c: c.name)
            ],
        }

    prefix = sec if sec.startswith("docs/") else f"docs/{sec}"
    prefix = prefix.rstrip("/")
    pages: list[dict[str, Any]] = []
    children: dict[str, int] = {}
    depth = len(prefix.split("/"))
    for path, page in sorted(index.corpus.pages.items()):
        if not path.startswith(prefix + "/"):
            continue
        parts = path.split("/")
        if len(parts) == depth + 1:
            pages.append(
                {"path": path, "title": page.title, "summary": page.summary}
            )
        else:
            children[parts[depth]] = children.get(parts[depth], 0) + 1

    if not pages and not children:
        return {
            "error": f"no docs section {sec!r}",
            "known": sorted(
                {
                    p.split("/")[2]
                    for p in index.corpus.pages
                    if p.startswith("docs/guide/") and p.count("/") > 2
                }
            ),
        }
    return {
        "section": prefix,
        "pages": pages,
        "subsections": [
            {"section": f"{prefix.removeprefix('docs/')}/{name}", "n_pages": n}
            for name, n in sorted(children.items())
        ],
    }


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


def waveflow_search(
    query: str,
    scope: str | None = "all",
    k: int | None = 8,
    include_generated: bool | None = False,
    root: str | None = None,
) -> dict[str, Any]:
    """BM25 over heading-sized chunks: the exact-words half of retrieval.

    *scope* is ``"all"``, ``"docs"`` or ``"examples"``.  Generated C++ is
    excluded unless *include_generated*; when it is included, every hit from it
    carries the never-hand-edit note.

    The optional arguments accept ``None`` as "use the default", because the
    registry declares these tools ``strict`` and a strict schema makes every
    property required -- so a model that wants the default passes null.
    """
    scope = scope or "all"
    k = 8 if k is None else k
    index = _idx(root)
    if scope not in ("all", "docs", "examples"):
        return {"error": f"scope must be all|docs|examples, got {scope!r}"}
    k = max(1, min(int(k), 50))

    # Over-fetch, then filter: the filters are cheap and a scoped query should
    # still return k hits rather than however many survived.
    wanted = k * 8 + 40
    want_kind = {"docs": "doc", "examples": "example"}.get(scope)
    ranked = [
        (index.chunks[i], s * _prior(index.chunks[i].path, index.chunks[i].kind))
        for i, s in index.bm25.search(query, k=wanted)
    ]
    ranked.sort(key=lambda cs: (-cs[1], cs[0].path, cs[0].start_line))

    hits: list[dict[str, Any]] = []
    per_path: dict[str, int] = {}
    for chunk, score in ranked:
        if want_kind is not None and chunk.kind != want_kind:
            continue
        if chunk.generated and not include_generated:
            continue
        # At most two sections from any one file.  Without this a single long
        # page fills the whole result set, and the second-best *page* -- which
        # is what the caller is actually choosing between -- never appears.
        if per_path.get(chunk.path, 0) >= _MAX_PER_PATH:
            continue
        per_path[chunk.path] = per_path.get(chunk.path, 0) + 1
        hit: dict[str, Any] = {
            "path": chunk.path,
            "heading": chunk.heading,
            "lines": [chunk.start_line, chunk.end_line],
            "snippet": chunk.snippet(),
            "score": round(score, 3),
        }
        if chunk.title:
            hit["title"] = chunk.title
        if chunk.example:
            hit["example"] = chunk.example
        also = index.corpus.duplicates.get(chunk.path)
        if also:
            hit["also_at"] = list(also)
        if chunk.generated:
            hit["note"] = generated_note(chunk.path, index.corpus.tracked)
        hits.append(hit)
        if len(hits) >= k:
            break

    return {"query": query, "scope": scope, "hits": hits}


# ---------------------------------------------------------------------------
# find_usage
# ---------------------------------------------------------------------------


def waveflow_find_usage(
    symbol: str,
    include_generated: bool | None = False,
    root: str | None = None,
) -> dict[str, Any]:
    """Every place *symbol* is used, grouped by example.

    Exact lookup, not ranking: "show me ``HostActivated`` in use" should return
    the four subclasses, not the pages that discuss it.  A miss returns fuzzy
    suggestions rather than an empty list, because the usual miss is a name
    remembered slightly wrong.
    """
    index = _idx(root)
    uses = index.usage.find(symbol)
    if not include_generated:
        tracked = index.corpus.tracked
        uses = [u for u in uses if not generated_note(u.path, tracked)]
    if not uses:
        return {
            "symbol": symbol,
            "found": False,
            "suggestions": index.usage.suggest(symbol),
        }

    grouped: dict[str, list[dict[str, Any]]] = {}
    for u in uses:
        entry: dict[str, Any] = {"path": u.path, "line": u.line, "how": u.how}
        if u.context:
            entry["in"] = u.context
        note = generated_note(u.path, index.corpus.tracked)
        if note:
            entry["note"] = note
        grouped.setdefault(u.example or "(other)", []).append(entry)
    for group in grouped.values():
        group.sort(key=lambda e: (e["path"], e["line"]))

    return {
        "symbol": symbol,
        "found": True,
        "n_uses": len(uses),
        "by_example": {name: grouped[name] for name in sorted(grouped)},
    }


# ---------------------------------------------------------------------------
# examples
# ---------------------------------------------------------------------------


def waveflow_list_examples(root: str | None = None) -> dict[str, Any]:
    """Every example card.

    The list is exactly the docs TOC (D5).  Directories under ``examples/``
    that no TOC page covers are deliberately absent: an example offered here is
    one an agent may copy, and the TOC is where that judgement already lives.
    """
    index = _idx(root)
    return {
        "examples": [
            c.to_dict(with_files=False)
            for c in sorted(index.cards.values(), key=lambda c: c.name)
        ]
    }


def waveflow_get_example(
    name: str, file: str | None = None, root: str | None = None
) -> dict[str, Any]:
    """The card plus the file list, or one **whole** file from the example."""
    index = _idx(root)
    card = index.cards.get(name)
    if card is None:
        from difflib import get_close_matches

        return {
            "error": f"no example {name!r}",
            "known": sorted(index.cards),
            "did_you_mean": get_close_matches(name, index.cards, n=3, cutoff=0.5),
        }

    if file is None:
        return {"example": card.to_dict()}

    known = {entry["path"]: entry for entry in card.files}
    rel = file if file in known else None
    if rel is None:
        # Accept a bare filename or a path relative to the example directory.
        matches = [
            p
            for p in known
            if p.endswith("/" + file.lstrip("/")) or Path(p).name == file
        ]
        if len(matches) == 1:
            rel = matches[0]
        elif matches:
            return {"error": f"{file!r} is ambiguous", "candidates": sorted(matches)}
        else:
            return {
                "error": f"{file!r} is not part of example {name!r}",
                "files": sorted(known),
            }

    out: dict[str, Any] = {
        "example": name,
        "path": rel,
        "content": index.corpus.read(rel),
    }
    note = generated_note(rel, index.corpus.tracked)
    if note:
        out["note"] = note
    return out


# ---------------------------------------------------------------------------
# get_doc
# ---------------------------------------------------------------------------


def waveflow_get_doc(
    path: str, heading: str | None = None, root: str | None = None
) -> dict[str, Any]:
    """A whole doc page, or one section of it."""
    index = _idx(root)
    rel = path.strip().lstrip("/")
    if rel not in index.corpus.pages:
        candidates = [p for p in index.corpus.pages if p.endswith("/" + rel) or p == rel]
        if len(candidates) == 1:
            rel = candidates[0]
        else:
            from difflib import get_close_matches

            return {
                "error": f"no doc page {path!r}",
                "did_you_mean": get_close_matches(
                    rel, list(index.corpus.pages), n=5, cutoff=0.4
                ),
            }

    page = index.corpus.pages[rel]
    if heading is None:
        return {
            "path": rel,
            "title": page.title,
            "summary": page.summary,
            "content": index.corpus.read(rel),
        }

    want = heading.strip().lower()
    for chunk in index.chunks:
        if chunk.path == rel and chunk.heading.strip().lower() == want:
            return {
                "path": rel,
                "title": page.title,
                "heading": chunk.heading,
                "lines": [chunk.start_line, chunk.end_line],
                "content": chunk.text,
            }
    return {
        "error": f"no heading {heading!r} on {rel}",
        "headings": [c.heading for c in index.chunks if c.path == rel],
    }
