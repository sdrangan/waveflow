"""Guard the committed docs against editor and formatter damage.

Every check here exists because the damage it detects **actually happened** and was caught by eye
rather than by anything mechanical.  Three separate sessions lost content to an editor that reflowed
markdown tables and, once, inserted stray keystrokes into prose.

Why a test rather than an editor setting: `editor.formatOnSave` was *already* `false` for markdown when
the worst of it occurred.  A table formatter hooks the format-document provider, so it also fires on an
explicit Format Document — easy to trigger with queued keystrokes while the editor is hung — and no
setting whatsoever prevents a stray keypress landing in a sentence.  Settings reduce the exposure; only
a check closes it.

Each failure mode is silent in review: a broken attribute list still renders, a table with an eaten
space still renders, and a `.OK` inside a word looks like a typo nobody typed.  That is exactly the
class of thing worth spending a test on.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

#: The CPython/VS Code habit of reflowing a table by re-padding cells miscounts the display width of
#: non-ASCII characters and drops a space while doing it.  Every corrupted row found so far contained
#: one — ``≥``, ``…``, ``—``.
_OPEN_CODE = re.compile(r"(?<=\w)`")
_OPEN_BOLD = re.compile(r"(?<=\w)\*\*")
#: Single-asterisk emphasis.  A separate pattern from bold because the two are counted on
#: different views of the line — see :func:`_eaten_spaces`.
_OPEN_EM = re.compile(r"(?<=\w)\*(?!\*)")

#: A kramdown attribute list must sit immediately above the block it styles.
_ATTR_LIST = re.compile(r"^\{:\s*\.\w+\s*\}\s*$")

#: Trees exempt from :func:`test_relative_links_resolve`, each for a stated reason rather than because
#: it was inconvenient:
#:
#: * ``plans/`` are working notes.  A plan routinely cites another that was finished and deleted, and
#:   that is a true record of how the work went, not rot to repair.
#: * ``examples/_archive/`` is archived by definition — its links point at the world as it was.
#: * ``waveflow/mcp/corpus/`` is a snapshot corpus, regenerated wholesale rather than edited.
#:
#: Everything a reader is actually pointed at — ``docs/`` and the live example READMEs — is checked.
_LINK_CHECK_SKIP = ("plans/", "examples/_archive/", "waveflow/mcp/corpus/")


def _tracked_markdown() -> list[Path]:
    """Every markdown file that is committed **or about to be**, that still exists on disk.

    ``git ls-files`` alone reads the *index*, which has two failure modes this guard cannot afford:

    * a **new** page is invisible until it is staged — so an entire docs section can be written,
      reviewed and merged without a single check running on it.  That happened: six example pages and
      five guide pages went unchecked, and the first thing this saw when they were finally staged was
      a broken anchor;
    * a **deleted** page is still listed until the deletion is staged, so the guard crashes reading a
      file that is gone.

    Adding untracked-but-not-ignored files and filtering to what exists closes both.  Ignored paths
    stay out, so build output and vendored trees are still skipped.
    """
    def _ls(*args: str) -> list[str]:
        out = subprocess.run(["git", "ls-files", *args, "*.md"], cwd=REPO,
                             capture_output=True, text=True, check=True)
        return out.stdout.split()

    paths = {REPO / p for p in _ls() + _ls("--others", "--exclude-standard")}
    return sorted(p for p in paths if p.is_file())


@pytest.fixture(scope="module")
def md_files() -> list[Path]:
    files = _tracked_markdown()
    assert files, "no tracked markdown found — is this a git checkout?"
    return files


def _lines(p: Path) -> list[str]:
    return p.read_text(encoding="utf-8").splitlines()


def _rel(p: Path) -> str:
    return p.relative_to(REPO).as_posix()


def test_frontmatter_starts_on_line_one(md_files):
    """A stray character above the frontmatter stops Jekyll parsing it at all.

    Seen for real: two backticks landed on line 1 of a guide page, which would have silently dropped
    its title, parent and nav_order.
    """
    broken = []
    for p in md_files:
        lines = _lines(p)
        head = "\n".join(lines[:12])
        if "title:" in head and (not lines or lines[0].strip() != "---"):
            broken.append(f"{_rel(p)}:1 (line 1 is {lines[0][:40]!r} — expected '---')")
    assert not broken, "frontmatter must start on line 1:\n  " + "\n  ".join(broken)


def test_attribute_lists_bind_to_a_block(md_files):
    """``{: .note }`` followed by a blank line silently stops styling the block below it.

    A formatter that "tidies" spacing introduces exactly this, and the page still renders — just
    without the callout, so nothing looks wrong enough to notice.
    """
    broken = []
    for p in md_files:
        lines = _lines(p)
        for i, ln in enumerate(lines[:-1]):
            if _ATTR_LIST.match(ln) and not lines[i + 1].strip():
                broken.append(f"{_rel(p)}:{i + 1} ({ln.strip()} followed by a blank line)")
    assert not broken, ("a kramdown attribute list must be immediately above its block:\n  "
                        + "\n  ".join(broken))


#: An inline code span.  Its *contents* are literal text to the renderer, so a ``**`` inside one is
#: not a bold delimiter and must not be counted as if it were.
_CODE_SPAN = re.compile(r"`[^`]*`")


def _mask_code_spans(line: str) -> str:
    """Blank out the inside of every code span, preserving length so offsets still line up.

    Without this, documenting Python's ``**kwargs`` breaks the parity count: a cell containing
    `` `**axes` `` leaves an odd number of ``**`` to the left of everything after it, so the next
    legitimate bold pair reads as an unclosed opening delimiter glued to a word.  That is a false
    positive, and a check with false positives gets suppressed — after which it protects nothing.
    """
    return _CODE_SPAN.sub(lambda m: "`" + " " * (len(m.group(0)) - 2) + "`", line)


def _eaten_spaces(line: str) -> list[int]:
    """Positions where an *opening* inline delimiter is glued to the preceding word.

    Openness is decided by counting delimiters to the left: a backtick with an even number before it
    opens a span, an odd number closes one.  That distinction matters — ``` `RegField`s ``` is a
    deliberate pluralisation and must not be flagged, while ``become`hls::task` `` is damage.

    Emphasis delimiters are counted on a **code-masked** copy, since markup inside a code span is
    literal text rather than markup.
    """
    line = _mask_code_spans(line)
    out = []
    for m in _OPEN_CODE.finditer(line):
        if line[:m.start()].count("`") % 2 == 0:
            out.append(m.start())
    for m in _OPEN_BOLD.finditer(line):
        if line[:m.start()].count("**") % 2 == 0:
            out.append(m.start())
    # Emphasis is counted with bold blanked out to same-length spaces: bold owns its own
    # asterisks, so counting them here would flip the open/closed parity, and a length-changing
    # substitution would misreport the position.
    em = line.replace("**", "  ")
    for m in _OPEN_EM.finditer(em):
        if em[:m.start()].count("*") % 2 == 0:
            out.append(m.start())
    return sorted(out)


def test_table_rows_have_no_eaten_spaces(md_files):
    """A table formatter re-padding cells deleted the space before inline markup.

    Renders visibly wrong — the backtick appears literally instead of opening a code span — and it is
    the single most common form this damage has taken.
    """
    broken = []
    for p in md_files:
        for i, ln in enumerate(_lines(p), 1):
            if not ln.lstrip().startswith("|"):
                continue
            for pos in _eaten_spaces(ln):
                broken.append(f"{_rel(p)}:{i}  …{ln[max(0, pos - 24):pos]}<<<{ln[pos:pos + 16]}…")
    assert not broken, ("a space was eaten before an opening inline delimiter:\n  "
                        + "\n  ".join(broken))


def test_the_pluralisation_idiom_is_not_flagged():
    """Guard the guard: the detector must not fire on legitimate ```Code`s`` pluralisation.

    A check with false positives gets suppressed, and then it protects nothing.
    """
    assert _eaten_spaces("| `RegMap` | Ordered collection of `RegField`s; owns the values |") == []
    assert _eaten_spaces("| a **bold** cell | and `code` here |") == []
    # Python kwargs in a code span: the ** inside is literal text, not a bold delimiter.  Counting it
    # left the next legitimate bold pair looking unclosed — a false positive that would have taught
    # someone to suppress the check.
    assert _eaten_spaces("| `**axes` | `name=values`. **Declaration order** — the outer loop |") == []
    assert _eaten_spaces("| `f(**kw)` | passes **everything** through |") == []
    # ...and it must still catch the real thing.
    assert _eaten_spaces("| which children become`hls::task`s |")
    assert _eaten_spaces("| stored in | the committed**platform library** |")


# ---------------------------------------------------------------------------
# Anchors — a link into a heading that no longer exists
# ---------------------------------------------------------------------------

#: kramdown lets a heading name its own anchor: ``## Float {#float}``.  That id wins outright, and
#: slugging the literal braces instead is how this check first produced a false positive.
_EXPLICIT_ID = re.compile(r"\{#([A-Za-z0-9_-]+)\}\s*$")


def _slug(heading: str) -> str:
    """The GitHub/kramdown slug for one heading's text.

    Lowercase, drop everything outside ``[a-z0-9 _-]``, spaces to hyphens.  Underscores *survive* —
    emulating that wrongly turned every ``m_axi`` heading into a phantom break.  The trailing hyphen a
    dropped emoji leaves behind is normalised away on both sides, so a heading marked ``… ✅`` still
    matches a link written without it.
    """
    return re.sub(r"[^a-z0-9 _-]", "", heading.lower().replace("`", "")).replace(" ", "-").strip("-")


def _heading_anchors(text: str) -> set[str]:
    """Every anchor a page's headings define.

    **A heading inside a blockquote is still a heading.**  The repo writes callout boxes as
    ``> ### Warning ...`` (``plans/mcp_fir.md``, ``plans/adc_model.md``,
    ``docs/examples/interleaver/pytiming.md``), and GitHub renders those as ``<h3>`` inside a
    ``<blockquote>`` and anchors them like any other.  An extractor anchored at ``^#`` sees none of
    them and reports every link into one as dead — which is a false positive, and a check with false
    positives gets suppressed, after which it protects nothing.
    """
    out = set()
    for h in re.findall(r"^(?:>\s*)*#{1,6}\s+(.*?)\s*$", text, re.M):
        explicit = _EXPLICIT_ID.search(h)
        if explicit:
            out.add(explicit.group(1).lower())
            h = h[:explicit.start()]
        slug = _slug(h)
        if slug:
            out.add(slug)
    return out


def test_anchors_point_at_headings_that_exist(md_files):
    """A renamed heading silently orphans every link into it — the page still renders.

    Both directions are checked, because the *same-page* form was a blind spot: a cross-file check
    passed while `[res_types](#the-counter-vocabulary)` sat dead in the same file it pointed at,
    orphaned by a rename two commits earlier.
    """
    anchors = {p: _heading_anchors(p.read_text(encoding="utf-8")) for p in md_files}
    by_path = {p.resolve(): p for p in md_files}

    broken = []
    for p in md_files:
        text = p.read_text(encoding="utf-8")
        for i, line in enumerate(text.splitlines(), 1):
            # same-page: [text](#anchor)
            for anc in re.findall(r"\]\(#([A-Za-z0-9_-]+)\)", line):
                if anc.lower().strip("-") not in anchors[p]:
                    broken.append(f"{_rel(p)}:{i} -> #{anc}  (no such heading on this page)")
            # cross-file: [text](./other.md#anchor)
            for rel, anc in re.findall(r"\]\((\.\.?/[^)#]+\.md)#([A-Za-z0-9_-]+)\)", line):
                tgt = (p.parent / rel).resolve()
                if tgt not in by_path:
                    continue                     # covered by test_relative_links_resolve
                if anc.lower().strip("-") not in anchors[by_path[tgt]]:
                    broken.append(f"{_rel(p)}:{i} -> {rel}#{anc}  (no such heading there)")
    assert not broken, "links point at headings that do not exist:\n  " + "\n  ".join(broken)


def test_relative_links_resolve(md_files):
    """A relative link must point at a file that exists.

    This was a genuine hole rather than a hypothetical one.  `test_anchors_point_at_headings_that_exist`
    skips a target it cannot find, with the comment that the file link is "covered elsewhere" -- and it
    was not.  Moving five pages out of `guide/calib` broke thirteen files' worth of links and the guard
    stayed green, because every broken link was to a file the anchor check then declined to resolve.

    A dead cross-page link is invisible in review: the page still renders, and only a reader who
    follows it finds out.  That is exactly the failure a guard is for.
    """
    broken = []
    for p in md_files:
        if any(part in _rel(p) for part in _LINK_CHECK_SKIP):
            continue
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            # Every relative target, not just `.md`.  These docs link into the source tree on
            # purpose -- 419 such links, all of which resolve -- so a renamed module rots them
            # exactly the way a moved page rots a cross-page link, and silently for the same reason.
            # A directory target is legitimate: `[Resource Models](../resource_model/)`.
            #
            # And a BARE target is legitimate too -- `[RTL simulation](rtlsim.md)`, a same-folder
            # sibling, which this file's own examples use throughout.  It was NOT matched until now,
            # and that hole cost four dead links: re-foldering guide/interface into primitive/ and
            # derived/ rewrote every `./stream.md` and left `](stream.md)` in a table pointing at a
            # page that had moved.  The guard stayed green while the links were dead, which is the
            # exact failure this test was written against -- one form of it simply had no pattern.
            for rel in re.findall(r"\]\(([^)#\s]+)(?:#[^)]*)?\)", line):
                if rel.startswith(("http://", "https://", "mailto:", "/")):
                    continue                     # absolute: not ours to resolve
                tgt = (p.parent / rel).resolve()
                if not (tgt.is_file() or tgt.is_dir()):
                    broken.append(f"{_rel(p)}:{i} -> {rel}")
    assert not broken, "relative links point at files that do not exist:\n  " + "\n  ".join(broken)


#: Front-matter keys this file cares about.  A hand-rolled reader rather than a YAML dependency:
#: the values wanted are all one-line scalars, and a page whose front matter does not parse is
#: already caught by :func:`test_frontmatter_starts_on_line_one`.
_FM_KEY = re.compile(r"^(title|parent|grand_parent):\s*(.*?)\s*$")


def _front_matter(p: Path) -> dict[str, str]:
    lines = _lines(p)
    if not lines or lines[0].strip() != "---":
        return {}
    fm = {}
    for ln in lines[1:]:
        if ln.strip() == "---":
            break
        m = _FM_KEY.match(ln)
        if m:
            fm[m.group(1)] = m.group(2).strip("\"'")
    return fm


def test_no_child_binds_to_an_ambiguous_parent_title(md_files):
    """Just the Docs resolves ``parent:`` by matching the **title string**, not the path.

    So two pages sharing a title are indistinguishable to the nav, and a child naming that title as
    its ``parent:`` binds to whichever one the theme happens to pick.  This repo shipped exactly that:
    two pages titled ``Overview`` with seven children between them.

    The dangerous property is that the symptom appears only when the *set of candidates changes* — a
    page renamed, a page added, a section re-foldered — so the mis-binding lands in a commit that
    looks unrelated to it.  ``grand_parent:`` disambiguates, and three titles currently rely on that
    (``HLS``, ``Overview``, ``Python``).  This asserts every child of an ambiguous title keeps it.
    """
    pages = {p: _front_matter(p) for p in md_files if _rel(p).startswith("docs/")}
    pages = {p: fm for p, fm in pages.items() if fm.get("title")}

    holders: dict[str, list[Path]] = {}
    for p, fm in pages.items():
        holders.setdefault(fm["title"], []).append(p)

    used_as_parent = {fm["parent"] for fm in pages.values() if fm.get("parent")}
    ambiguous = {t for t, ps in holders.items() if len(ps) > 1} & used_as_parent

    unguarded = []
    for p, fm in sorted(pages.items()):
        if fm.get("parent") in ambiguous and not fm.get("grand_parent"):
            held = ", ".join(sorted(_rel(h) for h in holders[fm["parent"]]))
            unguarded.append(f"{_rel(p)} (parent: {fm['parent']!r} — held by {held})")
    assert not unguarded, (
        "a child names a title held by more than one page and does not say which one:\n  "
        + "\n  ".join(unguarded))
