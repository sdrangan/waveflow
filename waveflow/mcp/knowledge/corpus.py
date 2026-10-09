"""The corpus: which files are in it, and how each one is cut into chunks.

Two rules shape this module.

**Find in chunks, read whole.**  Search returns pointers -- file, heading, line
range, a snippet -- and reading returns whole files.  So chunks exist only to be
ranked; nothing downstream reassembles them.

**The examples in the corpus are exactly the ones in the docs TOC** (decision
D5).  An example is offered to an agent as a model to copy, and some
directories under ``examples/`` are older experiments that should not be
copied.  The TOC -- ``docs/examples/<doc>/index.md`` -- is the curation that
already exists, so each TOC page names its source directory in an
``example_dir:`` front-matter key and that is the whole list.  Two doc names
differ from their directories (``memcpy``, ``firblock``), which is
exactly why the key is needed rather than inferring the path from the name.

What is *hand-written* in an example and what is *build output* is decided by
git, not by a path pattern: an untracked file under an example directory --
``gen/`` C++, a copied support header, a scratch sandbox -- is indexed but
tagged, kept out of search by default, and carries a note saying what it is.
An agent may still read it to see what codegen produces.  The repo's
``.gitignore`` already draws that line carefully (``examples/mem_copy/include/``
ignores four copied task headers while leaving the hand-written ones tracked),
so reusing it beats re-deriving it.
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from waveflow.mcp.knowledge.roots import extra_roots, repo_root

__all__ = [
    "Chunk",
    "DocPage",
    "ExampleSource",
    "Corpus",
    "load_corpus",
    "parse_front_matter",
    "generated_note",
    "TOC_DIR",
]

#: The docs tree whose ``index.md`` pages define the example list (D5).
TOC_DIR = "docs/examples"

#: Extensions worth indexing, and how each is cut up.  Anything else under a
#: source root is skipped -- notebooks, binaries, VCD dumps, JSON fixtures.
_MARKDOWN = {".md"}
_PYTHON = {".py"}
_CPP = {".h", ".hpp", ".tpp", ".cpp", ".cc", ".cxx", ".c"}
_PLAIN = {".tcl", ".sh", ".bat"}
_INDEXED = _MARKDOWN | _PYTHON | _CPP | _PLAIN

#: Whole-file chunks are split at roughly this many lines, on a blank line.
_SPLIT_LINES = 200

_FM_LINE = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*):\s*(.*?)\s*$")
_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*$")


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Chunk:
    """One ranked unit: a doc section, a Python symbol, or a slice of a file."""

    path: str            #: repo-relative, posix separators
    kind: str            #: ``"doc"`` or ``"example"``
    heading: str         #: section heading, symbol name, or the bare filename
    start_line: int      #: 1-based, inclusive
    end_line: int        #: 1-based, inclusive
    text: str
    title: str = ""      #: the page title, for doc chunks
    summary: str = ""    #: the page summary, for doc chunks
    example: str = ""    #: the TOC example this belongs to, for example chunks
    generated: bool = False

    def snippet(self, lines: int = 3) -> str:
        """The first *lines* non-blank lines of the chunk body."""
        body = [ln for ln in self.text.splitlines() if ln.strip()]
        # Drop the heading line itself; the caller already shows `heading`.
        if body and _HEADING.match(body[0]):
            body = body[1:]
        return "\n".join(body[:lines])


@dataclass(frozen=True)
class DocPage:
    """A markdown page under ``docs/``, with its Just-the-Docs front matter."""

    path: str
    title: str
    summary: str
    parent: str = ""
    grand_parent: str = ""
    nav_order: int | None = None
    example_dir: str = ""

    @property
    def section(self) -> str:
        """The top-level docs section: ``guide`` or ``examples``."""
        parts = self.path.split("/")
        return parts[1] if len(parts) > 2 else ""


@dataclass(frozen=True)
class ExampleSource:
    """A TOC example: its doc page, its source directory, and its files."""

    name: str                  #: the doc directory name, e.g. ``memcpy``
    example_dir: str           #: repo-relative source dir, e.g. ``examples/mem_copy``
    doc_path: str              #: ``docs/examples/<name>/index.md``
    title: str
    summary: str
    files: tuple[str, ...] = ()       #: source files, repo-relative
    doc_pages: tuple[str, ...] = ()   #: every page under ``docs/examples/<name>/``
    extra_files: tuple[str, ...] = () #: files the docs link to outside ``example_dir``


# ---------------------------------------------------------------------------
# Front matter
# ---------------------------------------------------------------------------


def parse_front_matter(text: str) -> tuple[dict[str, str], int]:
    """Return the YAML-ish front matter of *text* and the line it ends on.

    Deliberately not a YAML parse: every key wanted here is a one-line scalar,
    and a page whose front matter has some structure this misses should still
    be indexed rather than crash the build.  ``tests/docs`` makes the same
    trade-off for the same reason.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, 0
    out: dict[str, str] = {}
    for i, line in enumerate(lines[1:], start=2):
        if line.strip() == "---":
            return out, i
        m = _FM_LINE.match(line)
        if m:
            value = m.group(2)
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            out[m.group(1)] = value
    return out, 0


def _first_paragraph(body: str) -> str:
    """The first prose paragraph of *body*, for pages with no ``summary:``."""
    para: list[str] = []
    for line in body.splitlines():
        s = line.strip()
        if not s:
            if para:
                break
            continue
        if s.startswith(("#", "{:", "|", "```", "<!--", "---")):
            if para:
                break
            continue
        para.append(s)
    return " ".join(para)[:400]


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------


def _split_long(
    lines: list[str], start: int, limit: int = _SPLIT_LINES
) -> list[tuple[int, int]]:
    """Cut ``lines`` into ``(start_line, end_line)`` spans of about *limit*.

    The cut lands on the next blank line at or after the limit, so a chunk
    boundary never falls inside a function body when one can be avoided.
    """
    n = len(lines)
    if n <= limit:
        return [(start, start + n - 1)]
    spans: list[tuple[int, int]] = []
    i = 0
    while i < n:
        j = min(i + limit, n)
        while j < n and lines[j].strip():
            j += 1
        spans.append((start + i, start + j - 1))
        i = j
        while i < n and not lines[i].strip():
            i += 1
    return spans


def _chunk_markdown(
    rel: str,
    text: str,
    *,
    kind: str,
    page: DocPage | None,
    example: str,
    generated: bool,
) -> list[Chunk]:
    """Cut a markdown file at its ``##``/``###`` headings."""
    lines = text.splitlines()
    fm, fm_end = parse_front_matter(text)
    title = page.title if page else fm.get("title", Path(rel).stem)
    summary = page.summary if page else fm.get("summary", "")

    # Section boundaries: every h2/h3 outside a fenced code block.
    bounds: list[tuple[int, str]] = []
    fenced = False
    for i in range(fm_end, len(lines)):
        line = lines[i]
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        if fenced:
            continue
        m = _HEADING.match(line)
        if m and len(m.group(1)) in (2, 3):
            bounds.append((i, m.group(2)))

    spans: list[tuple[int, int, str]] = []
    lead_end = bounds[0][0] if bounds else len(lines)
    if lead_end > fm_end:
        spans.append((fm_end, lead_end, title))
    for n, (i, head) in enumerate(bounds):
        end = bounds[n + 1][0] if n + 1 < len(bounds) else len(lines)
        spans.append((i, end, head))

    out: list[Chunk] = []
    for lo, hi, head in spans:
        body = "\n".join(lines[lo:hi])
        if not body.strip():
            continue
        out.append(
            Chunk(
                path=rel,
                kind=kind,
                heading=head,
                start_line=lo + 1,
                end_line=hi,
                text=body,
                title=title,
                summary=summary,
                example=example,
                generated=generated,
            )
        )
    return out


def _chunk_python(rel: str, text: str, *, example: str, generated: bool) -> list[Chunk]:
    """Cut a Python file at its top-level ``class`` and ``def``.

    A syntax error is not fatal: the file still belongs in the corpus, so it
    falls back to the whole-file split.  Generated and half-written sources are
    both real.
    """
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return _chunk_plain(rel, text, example=example, generated=generated)

    lines = text.splitlines()
    tops = [
        n
        for n in tree.body
        if isinstance(n, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    out: list[Chunk] = []

    # The module preamble -- docstring, imports, module-level constants -- is a
    # chunk of its own.  It is where an example says which Waveflow names it
    # uses, which is exactly what a "show me X in use" query is after.
    head_end = (tops[0].lineno - 1) if tops else len(lines)
    if any(ln.strip() for ln in lines[:head_end]):
        for lo, hi in _split_long(lines[:head_end], 1):
            out.append(
                Chunk(
                    path=rel,
                    kind="example",
                    heading=f"{Path(rel).name} (module)",
                    start_line=lo,
                    end_line=hi,
                    text="\n".join(lines[lo - 1 : hi]),
                    example=example,
                    generated=generated,
                )
            )

    for n, node in enumerate(tops):
        lo = min((d.lineno for d in node.decorator_list), default=node.lineno)
        hi = node.end_lineno or lo
        # Reach to the next top-level definition so module-level code sitting
        # between two of them is not dropped.
        nxt = tops[n + 1] if n + 1 < len(tops) else None
        if nxt is not None:
            nxt_lo = min((d.lineno for d in nxt.decorator_list), default=nxt.lineno)
            hi = max(hi, nxt_lo - 1)
        else:
            hi = max(hi, len(lines))
        for a, b in _split_long(lines[lo - 1 : hi], lo):
            out.append(
                Chunk(
                    path=rel,
                    kind="example",
                    heading=node.name,
                    start_line=a,
                    end_line=b,
                    text="\n".join(lines[a - 1 : b]),
                    example=example,
                    generated=generated,
                )
            )
    return out


def _chunk_plain(rel: str, text: str, *, example: str, generated: bool) -> list[Chunk]:
    """Whole file, split at about 200 lines.  C++, TCL, shell."""
    lines = text.splitlines()
    name = Path(rel).name
    out: list[Chunk] = []
    spans = _split_long(lines, 1)
    for n, (lo, hi) in enumerate(spans):
        head = name if len(spans) == 1 else f"{name} [{n + 1}/{len(spans)}]"
        body = "\n".join(lines[lo - 1 : hi])
        if not body.strip():
            continue
        out.append(
            Chunk(
                path=rel,
                kind="example",
                heading=head,
                start_line=lo,
                end_line=hi,
                text=body,
                example=example,
                generated=generated,
            )
        )
    return out


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

#: Caches and VCS metadata.  Skipped under every root, docs included.
_SKIP_ALWAYS = frozenset(
    {
        "__pycache__",
        ".git",
        ".ipynb_checkpoints",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".venv",
        "venv",
        "node_modules",
        "htmlcov",
        "dist",
        "_archive",
    }
)

#: Directory names that hold run artefacts under a *source* root.  Not applied
#: to ``docs/``: these are ordinary English words, and a docs tree uses them as
#: subject names.  ``docs/guide/build/`` is the build-system section, and
#: skipping it by name made a whole chapter of the guide unsearchable.
#:
#: ``gen`` is in neither list: generated code is indexed and tagged instead, so
#: an agent can read what codegen produced.
_SKIP_IN_SOURCE = _SKIP_ALWAYS | {
    "logs",
    "results",
    "vcd",
    "data",
    "build",
    "figs",
    "figures",
    # Per-configuration build trees (examples/vitis_fft/work/L<n>/: one full include/gen/xsi copy
    # each, hundreds of MB once traced).  Indexing them took the index build from ~2 s to ~5 s.
    "work",
    # A system build DAG's RTL runs (plans/system_dag.md: examples/<ex>/xsi_work/): the crossbar IP's
    # generated Verilog, XSI workspaces, traces.  Indexing one took the index build from ~2 s to ~11 s.
    "xsi_work",
}

#: Vitis/Vivado project trees, sweep output and the like.  Matched as suffixes
#: on a directory name so a new ``*_proj`` does not need a code change.
_SKIP_SUFFIXES = ("_proj", ".egg-info", "_out", "_output")


def _walk(base: Path, *, skip: frozenset[str] = _SKIP_IN_SOURCE) -> list[Path]:
    """Every indexable file under *base*, skipping artefact trees."""
    out: list[Path] = []
    stack = [base]
    while stack:
        d = stack.pop()
        try:
            entries = list(d.iterdir())
        except OSError:
            continue
        for e in entries:
            if e.is_dir():
                if e.name in skip or e.name.endswith(_SKIP_SUFFIXES):
                    continue
                stack.append(e)
            elif e.suffix.lower() in _INDEXED:
                out.append(e)
    return sorted(out)


@lru_cache(maxsize=4)
def _tracked(root: str) -> frozenset[str]:
    """Every git-tracked path under *root*, or an empty set outside a checkout.

    This is the corpus's hand-written/build-output line, and the repo already
    maintains it with care: ``.gitignore`` names ``examples/*/gen/``, the four
    copied task headers under ``examples/mem_copy/include/`` (leaving the
    genuinely hand-written ones tracked), and the interleaver DATAFLOW
    sandbox.  Re-deriving that from path patterns would get the mem_copy case
    wrong on the first try and stay wrong; asking git gets it exactly right and
    keeps getting it right when the ignore file changes.

    An empty set means "no checkout", and every file is then treated as
    tracked -- better to over-offer than to tag the whole corpus generated.
    """
    import subprocess

    try:
        out = subprocess.run(
            ["git", "-C", root, "ls-files", "-z"],
            # Never inherit stdin.  Inside the MCP server stdin is the
            # JSON-RPC pipe, and git blocked on it until the timeout: the
            # first index-backed tool call took 31.9 s instead of ~2 s, and
            # the timeout's empty set then un-tagged every generated file.
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return frozenset()
    return frozenset(out.stdout.decode("utf-8", "replace").split("\0")) - {""}


def generated_note(rel: str, tracked: frozenset[str]) -> str:
    """Why *rel* is kept out of search by default, or ``""`` when it is not."""
    if not tracked or rel in tracked:
        return ""
    if "/gen/" in f"/{rel}":
        return "generated; never hand-edit"
    return (
        "not tracked in git: build output or scratch, not a model to copy"
    )


_LINK = re.compile(r"\]\(([^)\s]+)\)")
_CODE_PATH = re.compile(r"`(examples/[A-Za-z0-9_./-]+)`")


def _linked_example_paths(root: Path, doc_paths: list[Path], own_dir: str) -> list[str]:
    """Files under ``examples/`` a TOC page points at from *outside* its own dir.

    D5 keeps everything else under ``examples/`` out of the corpus, but a page
    that tells the reader to go and look at a file has, by that act, curated it.
    Today that is ``examples/schemas/fixedpoint/`` (from ``basic_vec``); deriving
    the list from the pages means the next one costs nothing.
    """
    found: set[str] = set()
    for doc in doc_paths:
        try:
            text = doc.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        cands: list[str] = []
        for m in _LINK.finditer(text):
            target = m.group(1).split("#", 1)[0]
            if "examples/" not in target or target.startswith(("http://", "https://")):
                continue
            resolved = (doc.parent / target).resolve()
            try:
                cands.append(resolved.relative_to(root).as_posix())
            except ValueError:
                continue
        cands.extend(m.group(1) for m in _CODE_PATH.finditer(text))

        for rel in cands:
            rel = rel.rstrip("/")
            if not rel.startswith("examples/") or rel == own_dir:
                continue
            if own_dir and rel.startswith(own_dir + "/"):
                continue
            p = root / rel
            if p.is_file() and p.suffix.lower() in _INDEXED:
                found.add(rel)
            elif p.is_dir():
                found.update(f.relative_to(root).as_posix() for f in _walk(p))
    return sorted(found)


# ---------------------------------------------------------------------------
# The corpus
# ---------------------------------------------------------------------------


@dataclass
class Corpus:
    """Everything the knowledge tools read: pages, examples and chunks."""

    root: Path
    pages: dict[str, DocPage] = field(default_factory=dict)
    examples: dict[str, ExampleSource] = field(default_factory=dict)
    chunks: list[Chunk] = field(default_factory=list)
    #: repo-relative path -> the example it belongs to, for every indexed source
    file_owner: dict[str, str] = field(default_factory=dict)
    #: every git-tracked path, or empty outside a checkout.  See `_tracked`.
    tracked: frozenset[str] = frozenset()
    #: indexed path -> the other paths holding byte-identical content
    duplicates: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def read(self, rel: str) -> str:
        return (self.root / rel).read_text(encoding="utf-8", errors="replace")


def _toc_examples(root: Path) -> dict[str, ExampleSource]:
    """The TOC examples (D5), read from ``docs/examples/*/index.md``."""
    out: dict[str, ExampleSource] = {}
    toc = root / TOC_DIR
    if not toc.is_dir():
        return out
    for d in sorted(p for p in toc.iterdir() if p.is_dir()):
        index = d / "index.md"
        if not index.is_file():
            continue
        text = index.read_text(encoding="utf-8", errors="replace")
        fm, fm_end = parse_front_matter(text)
        example_dir = fm.get("example_dir", "").strip().rstrip("/")
        doc_pages = sorted(
            p.relative_to(root).as_posix() for p in d.rglob("*.md") if p.is_file()
        )
        files: tuple[str, ...] = ()
        extra: tuple[str, ...] = ()
        if example_dir and (root / example_dir).is_dir():
            files = tuple(
                f.relative_to(root).as_posix() for f in _walk(root / example_dir)
            )
            extra = tuple(
                _linked_example_paths(root, [root / p for p in doc_pages], example_dir)
            )
        body = "\n".join(text.splitlines()[fm_end:])
        out[d.name] = ExampleSource(
            name=d.name,
            example_dir=example_dir,
            doc_path=index.relative_to(root).as_posix(),
            title=fm.get("title", d.name),
            summary=fm.get("summary", "") or _first_paragraph(body),
            files=files,
            doc_pages=tuple(doc_pages),
            extra_files=extra,
        )
    return out


def _content_hash(p: Path) -> str:
    """SHA-1 of *p*, or ``""`` when it cannot be read."""
    import hashlib

    try:
        return hashlib.sha1(p.read_bytes()).hexdigest()
    except OSError:
        return ""


def _chunk_source(
    root: Path, rel: str, example: str, tracked: frozenset[str]
) -> list[Chunk]:
    p = root / rel
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    generated = bool(generated_note(rel, tracked))
    suffix = p.suffix.lower()
    if suffix in _PYTHON:
        return _chunk_python(rel, text, example=example, generated=generated)
    if suffix in _MARKDOWN:
        return _chunk_markdown(
            rel, text, kind="example", page=None, example=example, generated=generated
        )
    return _chunk_plain(rel, text, example=example, generated=generated)


def _build(root: Path) -> Corpus:
    corpus = Corpus(root=root)
    tracked = _tracked(str(root))
    corpus.tracked = tracked

    # --- docs -------------------------------------------------------------
    for base in (root / "docs" / "guide", root / "docs" / "examples"):
        if not base.is_dir():
            continue
        for f in _walk(base, skip=_SKIP_ALWAYS):
            if f.suffix.lower() not in _MARKDOWN:
                continue
            rel = f.relative_to(root).as_posix()
            text = f.read_text(encoding="utf-8", errors="replace")
            fm, fm_end = parse_front_matter(text)
            body = "\n".join(text.splitlines()[fm_end:])
            nav = fm.get("nav_order", "")
            page = DocPage(
                path=rel,
                title=fm.get("title", f.stem),
                summary=fm.get("summary", "") or _first_paragraph(body),
                parent=fm.get("parent", ""),
                grand_parent=fm.get("grand_parent", ""),
                nav_order=int(nav) if nav.lstrip("-").isdigit() else None,
                example_dir=fm.get("example_dir", ""),
            )
            corpus.pages[rel] = page
            corpus.chunks.extend(
                _chunk_markdown(
                    rel, text, kind="doc", page=page, example="", generated=False
                )
            )

    # --- examples (D5: the TOC list only) ---------------------------------
    corpus.examples = _toc_examples(root)
    seen: set[str] = set()
    # Content hash -> the path already indexed for it.  Six examples carry a
    # byte-identical copy of `streamutils_hls.h`, copied in from
    # `waveflow/build/` by the build; without this, a query about stream
    # framing returns the same file five times and nothing else.  Dedup by
    # content rather than by name so it also covers the next copied header.
    by_content: dict[str, str] = {}
    dupes: dict[str, list[str]] = {}
    # Two passes: every example claims its OWN directory first, and only then the files its pages
    # link to elsewhere.  In one pass, an example whose name sorts earlier claimed another example's
    # file just by linking to it (mm_fir's docs link stream_inband/poly.py), and the file's own example
    # lost it -- its usages were then credited to the wrong example.
    claims = [(ex, rel) for ex in corpus.examples.values() for rel in ex.files]
    claims += [(ex, rel) for ex in corpus.examples.values() for rel in ex.extra_files]
    for ex, rel in claims:
        if rel in seen:
            continue
        seen.add(rel)
        corpus.file_owner[rel] = ex.name
        digest = _content_hash(root / rel)
        first = by_content.get(digest) if digest else None
        if first is not None:
            dupes.setdefault(first, []).append(rel)
            continue
        if digest:
            by_content[digest] = rel
        corpus.chunks.extend(_chunk_source(root, rel, ex.name, tracked))
    corpus.duplicates = {k: tuple(v) for k, v in dupes.items()}

    # --- extra roots (a course's own examples) ----------------------------
    for extra in extra_roots():
        for f in _walk(extra):
            rel = f.relative_to(extra).as_posix()
            key = f"{extra.name}/{rel}"
            if key in seen:
                continue
            seen.add(key)
            corpus.chunks.extend(_chunk_source(extra, rel, extra.name, frozenset()))

    return corpus


@lru_cache(maxsize=4)
def _cached(root: str) -> Corpus:
    return _build(Path(root))


def load_corpus(root: Path | None = None, *, refresh: bool = False) -> Corpus:
    """Build (or reuse) the corpus for *root*.

    Cached per root: a server process builds once, and the tests can force a
    rebuild with ``refresh=True`` after writing a fixture tree.
    """
    base = Path(root) if root is not None else repo_root()
    if refresh:
        _cached.cache_clear()
    return _cached(str(base.resolve()))
