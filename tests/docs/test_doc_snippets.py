"""Run the ``python`` snippets on a docs page and diff them against their output fences.

**Why this exists.**  A code block in a guide is the one kind of documentation a reader will copy
verbatim, and it is also the kind that rots invisibly: a rename lands, the prose around the block is
updated because a human read it, and the block itself still says the old thing.  Nothing renders
wrong.  An earlier throwaway version of this harness found **four** stale blocks on pages whose own
author had just read past them, which is the whole argument — the failure is not that the blocks are
hard to check, it is that reading them is not checking them.

**The contract, which is page-level and opt-in.**  A page opts in with ``snippets: run`` in its front
matter.  Then:

* every ```` ```python ```` fence on the page is part of **one script**, concatenated in order.  A
  page tells one story, and its later blocks routinely use names its earlier blocks bound;
* a ```` ```text ```` fence that follows a python fence (prose in between is fine) is that script's
  **expected stdout**, and they are concatenated the same way;
* the script runs in a fresh namespace with the repo importable, and its stdout must match.

Opt-in rather than opt-out, and page-level rather than block-level, for one reason each.  Opt-in,
because most of the ~600 python fences in ``docs/`` are deliberate fragments — a method body, a
pragma, a signature — and a harness that failed on those would be turned off within a week.
Page-level, because a block that runs only in the company of the blocks above it is the normal case,
and marking each one individually would put the same fact in five places.

**Elision.**  A line that is exactly ``...`` in an expected block means *"skip ahead"*: the lines
after it must appear later in the output, in order.  Real output is often long and the interesting
part is the start and the end, and forcing a page to paste 200 lines to be checkable would trade one
kind of rot for another.

**Excusing one fence.**  An HTML comment ``<!-- snippet: skip -->`` on the line before a fence keeps
that fence out of the script.  It exists so a page that is 90% runnable can still be checked instead
of being excluded whole: the alternative is that one illustrative signature costs the page every
other block's coverage, which is how a harness ends up covering nothing.  It is a visible marker in
the source and greppable, so the exclusions can be counted rather than assumed.
"""
from __future__ import annotations

import io
import re
import subprocess
import sys
import types
from contextlib import redirect_stdout
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

#: A fence opener, capturing the language.  Trailing attributes are not used by this repo's fences.
_FENCE = re.compile(r"^```(\w*)\s*$")

#: The front-matter opt-in.
_OPT_IN = re.compile(r"^snippets:\s*run\s*$", re.M)

#: Languages a fence may use to hold expected output.
_OUTPUT_LANGS = ("text", "console", "output")

#: The marker that excuses the fence below it.  An HTML comment, so it is invisible in the
#: rendered page and greppable in the source.
_SKIP = "<!-- snippet: skip -->"


def _pages() -> list[Path]:
    """Every docs page that is committed **or about to be**.

    ``git ls-files`` alone reads the index, so a brand-new page is invisible until it is staged —
    which is precisely when its snippets have never been run.  The sibling
    ``test_markdown_integrity`` learned this the hard way (eleven unchecked pages reached review);
    adding untracked-but-not-ignored files closes the same hole here, and ignored paths stay out.
    """
    def _ls(*args: str) -> list[str]:
        out = subprocess.run(["git", "ls-files", *args, "docs/**/*.md"], cwd=REPO,
                             capture_output=True, text=True, check=True)
        return out.stdout.split()

    paths = {REPO / p for p in _ls() + _ls("--others", "--exclude-standard")}
    return sorted(p for p in paths if p.is_file())


def _front_matter(text: str) -> str:
    if not text.startswith("---\n"):
        return ""
    end = text.find("\n---\n", 3)
    return text[:end] if end != -1 else ""


def _blocks(text: str) -> list[tuple[str, str]]:
    """Every fenced block on the page as ``(language, body)``, in order.

    A block preceded by ``<!-- snippet: skip -->`` is reported with language ``"skip"``, so it
    leaves the script *and* cannot be mistaken for an output fence.
    """
    lines = text.splitlines()
    out, i = [], 0
    while i < len(lines):
        m = _FENCE.match(lines[i])
        if not m:
            i += 1
            continue
        lang, j = m.group(1) or "", i + 1
        while j < len(lines) and not lines[j].startswith("```"):
            j += 1
        if i and lines[i - 1].strip() == _SKIP:
            lang = "skip"
        out.append((lang, "\n".join(lines[i + 1:j])))
        i = j + 1
    return out


def _script_and_expected(text: str) -> tuple[str, str]:
    """The page's concatenated python script, and its concatenated expected stdout.

    An output fence counts only when a python fence came before it with no *other* python fence in
    between — otherwise a ``text`` block illustrating something unrelated (a directory listing, a
    generated header) would be mistaken for output nobody claimed to produce.
    """
    script, expected, saw_python = [], [], False
    for lang, body in _blocks(text):
        if lang == "python":
            script.append(body)
            saw_python = True
        elif lang in _OUTPUT_LANGS and saw_python:
            expected.append(body)
            saw_python = False
    return "\n\n".join(script), "\n".join(expected)


def _matches(expected: str, actual: str) -> str | None:
    """``None`` if *actual* satisfies *expected*, else a human-readable complaint.

    Exact line-for-line, except that a bare ``...`` skips ahead to the next line that matches.
    """
    exp = [ln.rstrip() for ln in expected.splitlines()]
    act = [ln.rstrip() for ln in actual.splitlines()]
    ai = 0
    i = 0
    while i < len(exp):
        line = exp[i]
        if line.strip() == "...":
            i += 1
            if i == len(exp):
                return None                       # trailing "..." swallows the rest
            nxt = exp[i]
            while ai < len(act) and act[ai] != nxt:
                ai += 1
            if ai == len(act):
                return f"after an elision, expected {nxt!r} never appeared"
            continue
        if ai >= len(act):
            return f"output ended early; expected {line!r}"
        if act[ai] != line:
            return (f"line {ai + 1} of the output is {act[ai]!r}, "
                    f"but the page says {line!r}")
        ai += 1
        i += 1
    return None


OPTED_IN = [p for p in _pages() if _OPT_IN.search(_front_matter(p.read_text(encoding="utf-8")))]


def _rel(p: Path) -> str:
    return p.relative_to(REPO).as_posix()


@pytest.mark.parametrize("page", OPTED_IN, ids=_rel)
def test_page_snippets_run_and_match_their_output(page: Path):
    """The page's python blocks run as one script and print what the page says they print."""
    text = page.read_text(encoding="utf-8")
    script, expected = _script_and_expected(text)
    assert script.strip(), (
        f"{_rel(page)} declares `snippets: run` but has no ```python fence. Remove the marker "
        f"rather than leaving a page that claims to be checked and is not.")

    # A REAL module object, registered in ``sys.modules``, not a bare dict.  ``@dataclass`` resolves
    # a class's annotations through ``sys.modules[cls.__module__].__dict__``, so a snippet defining
    # a dataclass — which is most of them, this being a dataclass-heavy codebase — fails with a bare
    # ``AttributeError: 'NoneType' object has no attribute '__dict__'`` that says nothing about the
    # page.  Registering the module is what makes a snippet behave like the file a reader would
    # paste it into.
    mod_name = "waveflow_doc_snippet__" + re.sub(r"\W", "_", _rel(page))
    module = types.ModuleType(mod_name)
    module.__file__ = str(page)
    buf = io.StringIO()
    old_path, prev = sys.path[:], sys.modules.get(mod_name)
    sys.path.insert(0, str(REPO))
    sys.modules[mod_name] = module
    try:
        with redirect_stdout(buf):
            exec(compile(script, f"<{_rel(page)}>", "exec"), module.__dict__)
    except Exception as exc:                       # noqa: BLE001 - the message is the point
        raise AssertionError(
            f"{_rel(page)}: its python blocks do not run as one script.\n"
            f"  {type(exc).__name__}: {exc}\n"
            f"Every ```python fence on an opted-in page is part of one script, in order. "
            f"Mark a fence that is a deliberate fragment with `{_SKIP}` on the line above it."
        ) from exc
    finally:
        sys.path[:] = old_path
        if prev is None:
            sys.modules.pop(mod_name, None)
        else:
            sys.modules[mod_name] = prev

    if not expected.strip():
        return                                     # runs, claims no output: that is a valid page

    complaint = _matches(expected, buf.getvalue())
    assert complaint is None, (
        f"{_rel(page)}: the output fence no longer matches what the code prints.\n"
        f"  {complaint}\n"
        f"--- the page says ---\n{expected}\n"
        f"--- it actually prints ---\n{buf.getvalue()}")


def test_the_harness_covers_something_and_reports_what_it_does_not():
    """A coverage floor, so the harness cannot quietly end up checking nothing.

    Not a ceiling: most python fences in ``docs/`` are deliberate fragments and are none of this
    test's business.  What this guards is the failure where a refactor drops every ``snippets: run``
    marker and the suite stays green while checking zero blocks.
    """
    assert len(OPTED_IN) >= 2, (
        f"only {len(OPTED_IN)} page(s) opt in to snippet checking. Adding `snippets: run` to a "
        f"page's front matter is what makes its code blocks checked rather than merely read.")


# ---------------------------------------------------------------------------
# The harness's own machinery, exercised directly
# ---------------------------------------------------------------------------
#
# `_matches` and the skip marker are the two pieces that decide whether a real failure is reported,
# and neither is exercised by a page that passes.  Untested machinery in a guard is worse than no
# guard, because the guard is trusted.

def test_elision_skips_ahead_but_still_anchors():
    assert _matches("a\n...\nd", "a\nb\nc\nd") is None
    assert _matches("a\n...", "a\nb\nc") is None, "a trailing ... swallows the rest"
    assert _matches("a\n...\nz", "a\nb\nc") is not None, "an anchor that never appears must fail"


def test_a_changed_line_is_reported_with_both_versions():
    complaint = _matches("total: 7", "total: 8")
    assert complaint is not None
    assert "'total: 8'" in complaint and "'total: 7'" in complaint, (
        "the message has to carry both, or a reader cannot tell which way the drift went")


def test_output_ending_early_is_not_a_pass():
    assert _matches("one\ntwo", "one") is not None


def test_an_output_fence_needs_a_python_fence_in_front_of_it():
    """A stray ``text`` block is not an output claim.

    Pages are full of ``text`` fences showing directory listings and generated headers.  Treating
    one as expected stdout would fail a page for output nobody said it produced.
    """
    page = "```text\nnot output\n```\n\n```python\nprint('hi')\n```\n\n```text\nhi\n```\n"
    script, expected = _script_and_expected(page)
    assert script == "print('hi')"
    assert expected == "hi", "only the fence AFTER a python block counts"


def test_the_skip_marker_takes_a_fence_out_of_the_script():
    """A fence marked ``<!-- snippet: skip -->`` leaves the script and cannot be read as output."""
    page = (f"```python\nx = 1\n```\n\n{_SKIP}\n```python\nthis is not python\n```\n\n"
            f"```python\nprint(x)\n```\n\n{_SKIP}\n```text\nnot an output claim\n```\n")
    script, expected = _script_and_expected(page)
    assert "this is not python" not in script, "the excused fence is still in the script"
    assert script == "x = 1\n\nprint(x)"
    assert expected == "", "an excused text fence is not expected output either"
