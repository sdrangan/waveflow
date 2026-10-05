"""tests/build/test_source_layout.py -- nothing generated is tracked (``plans/source_layout.md``).

The rule: every generated directory holds only generated files, is gitignored, and can be deleted
and rebuilt; everything authored lives outside them (hand-written C++ in ``src/``) and is tracked.
A tracked file in a generated directory is the thing the rule exists to prevent: an edit to it is
lost on the next build, a pull that changes its origin leaves it behind, and an agent cannot tell
it from source.

Examples not yet migrated are on :data:`NOT_YET_MIGRATED`.  The list may only shrink -- the second
test fails on an entry that no longer tracks anything, so a migration has to take its example off.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

#: Directories every build writes into and nothing authored may live in.
GENERATED_DIRS = ("include", "gen", "xsi")

#: Examples that still track generated files (census 2026-10-05).  Remove an example when it
#: migrates (plans/source_layout.md, S2); the list ends empty.
NOT_YET_MIGRATED = frozenset({
    "bram_access", "fir_block", "interleaver", "mem_copy", "mm_fir", "regmap", "rf_blk_delay",
    "rf_loopback", "rf_relayout", "rf_repeat_play", "rf_samp_buf_rx", "rf_samp_buf_tx",
    "rf_shot_rx", "rf_shot_tx", "state_toy", "vecmult",
})


def _tracked(*patterns: str) -> list[str]:
    try:
        out = subprocess.run(["git", "ls-files", "--", *patterns], cwd=REPO, check=True,
                             capture_output=True, text=True).stdout
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("not a git checkout")
    return [ln for ln in out.splitlines() if ln and not ln.startswith("examples/_archive/")]


def _tracked_text(path: str) -> str:
    """*path*'s text as git tracks it: the working copy, or the index's when it was deleted locally
    (a deletion not yet committed is still a tracked file)."""
    f = REPO / path
    if f.is_file():
        return f.read_text(encoding="utf-8", errors="replace")
    return subprocess.run(["git", "show", f":{path}"], cwd=REPO, capture_output=True, text=True,
                          encoding="utf-8", errors="replace").stdout


def _offenders() -> dict[str, list[str]]:
    """``{example: [tracked generated paths]}``.

    A root-level ``.tcl`` counts when ``render_tcl`` wrote it (it announces itself with
    ``WAVEFLOW_INFO``); hand-written drivers such as ``basic_vec/run.tcl`` do not.
    """
    paths = _tracked(*(f"examples/*/{d}/*" for d in GENERATED_DIRS))
    for tcl in _tracked("examples/*/*.tcl"):
        if "WAVEFLOW_INFO" in _tracked_text(tcl):
            paths.append(tcl)
    out: dict[str, list[str]] = {}
    for p in paths:
        out.setdefault(p.split("/")[1], []).append(p)
    return out


def test_no_generated_file_is_tracked():
    bad = {ex: files for ex, files in _offenders().items() if ex not in NOT_YET_MIGRATED}
    assert not bad, (
        "generated files are tracked (plans/source_layout.md): move anything hand-written into src/, "
        "`git rm` the rest (not --cached -- a leftover copy can shadow src/), and gitignore the "
        "directory:\n" + "\n".join(f"  {ex}: {', '.join(sorted(f)[:5])}"
                                    + (" ..." if len(f) > 5 else "") for ex, f in sorted(bad.items())))


def test_the_not_yet_migrated_list_only_shrinks():
    stale = sorted(NOT_YET_MIGRATED - set(_offenders()))
    assert not stale, f"these examples no longer track generated files; remove them from " \
                      f"NOT_YET_MIGRATED: {stale}"
