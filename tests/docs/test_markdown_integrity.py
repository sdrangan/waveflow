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

#: A kramdown attribute list must sit immediately above the block it styles.
_ATTR_LIST = re.compile(r"^\{:\s*\.\w+\s*\}\s*$")


def _tracked_markdown() -> list[Path]:
    out = subprocess.run(["git", "ls-files", "*.md"], cwd=REPO,
                         capture_output=True, text=True, check=True)
    return [REPO / p for p in out.stdout.split()]


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


def _eaten_spaces(line: str) -> list[int]:
    """Positions where an *opening* inline delimiter is glued to the preceding word.

    Openness is decided by counting delimiters to the left: a backtick with an even number before it
    opens a span, an odd number closes one.  That distinction matters — ``` `RegField`s ``` is a
    deliberate pluralisation and must not be flagged, while ``become`hls::task` `` is damage.
    """
    out = []
    for m in _OPEN_CODE.finditer(line):
        if line[:m.start()].count("`") % 2 == 0:
            out.append(m.start())
    for m in _OPEN_BOLD.finditer(line):
        if line[:m.start()].count("**") % 2 == 0:
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
    # ...and it must still catch the real thing.
    assert _eaten_spaces("| which children become`hls::task`s |")
    assert _eaten_spaces("| stored in | the committed**platform library** |")
