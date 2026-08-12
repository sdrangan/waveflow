"""The committed XSI workspaces must match the framework they were copied from.

``XsiHarnessStep`` copies the BFM library — ``xsi_bfm.h``, ``xsi_simobj.h``, ``xsi_channel.h``,
``xsi_bundle.h``, the loader, ``run.bat`` — from ``waveflow/build/xsi/`` into each example's ``xsi/``
directory.  Those copies are **build outputs that happen to be committed**, and nothing checked them,
so they drifted.  Two ways, both found on 2026-08-12:

- Every example's ``xsi_bfm.h`` was missing the platform switch that picks the xsim engine library by
  name, and hardcoded the **Windows** DLL instead.  A committed workspace on Linux named a file that
  does not exist there.
- ``examples/interleaver/xsi/run.bat`` still described ``interleaver_canon``, a top retired in
  2026-07.

**Why the ``-m xsi`` gates did not catch either.**  The gates regenerate the ``.f`` file list and force
a clean elaboration, but they do *not* re-run ``XsiHarnessStep`` — that lives in each example's build
DAG.  So the gates compile the **committed copies**, which means a change to the framework source is
not under test until someone regenerates.  The library the gates exercise and the library the
repository ships were two different files, and both were self-consistent enough to stay green.

That is the failure this module closes, and it is the same shape as the stale ``2835/3469`` cycle
numbers: a fact with no checker rots quietly, and every test stays green while it does.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_SRC = _REPO / "waveflow" / "build" / "xsi"
_EXAMPLES = _REPO / "examples"

#: ``#include "foo.h"`` — quoted form only.  An angle-bracket include is a system/Vivado header and is
#: not ours to ship.
_INCLUDE = re.compile(r'^\s*#\s*include\s*"([^"]+)"', re.M)


def _norm(p: Path) -> str:
    """File text with line endings normalized.

    The committed copies are CRLF and the source is LF (or the reverse, depending on who last touched
    them under ``core.autocrlf``).  That difference is real but is *not* drift in the sense this module
    cares about, and asserting on it would make the check fail for everyone on the wrong platform
    rather than for the person who changed the library.
    """
    return p.read_text(encoding="utf-8").replace("\r\n", "\n")


def _framework_files() -> set[str]:
    return {p.name for p in _SRC.iterdir() if p.is_file()}


def _workspaces() -> list[Path]:
    """Every example ``xsi/`` directory that holds a copy of the library."""
    return sorted(d for d in _EXAMPLES.glob("*/xsi") if (d / "xsi_bfm.h").is_file())


def test_there_are_workspaces_to_check():
    """The guard on the guard: a renamed directory would make every check below vacuous."""
    ws = _workspaces()
    assert len(ws) >= 3, f"expected several example XSI workspaces, found {[str(d) for d in ws]}"


@pytest.mark.parametrize("ws", _workspaces(), ids=lambda d: d.parent.name)
def test_committed_copies_match_the_framework_source(ws: Path):
    """Byte-identical (modulo line endings) to ``waveflow/build/xsi/``.

    Regenerate rather than hand-edit: the example copies are outputs.  If this fails after a
    deliberate library change, re-run the example's build (or copy the file) and commit the result —
    the point is that the two cannot silently disagree, not that the library cannot change.
    """
    names = _framework_files()
    checked = 0
    for f in sorted(ws.iterdir()):
        if not f.is_file() or f.name not in names:
            continue
        checked += 1
        assert _norm(f) == _norm(_SRC / f.name), (
            f"{f.relative_to(_REPO)} has drifted from waveflow/build/xsi/{f.name}. The XSI gates "
            f"compile THIS copy, not the source, so the drift is not cosmetic: the library under "
            f"test and the library shipped are different files.")
    assert checked, f"{ws.relative_to(_REPO)} holds no framework files to check"


@pytest.mark.parametrize("ws", _workspaces(), ids=lambda d: d.parent.name)
def test_every_framework_include_is_present_in_the_workspace(ws: Path):
    """A workspace must contain every framework header its own files include.

    Derived from the ``#include`` lines rather than a hardcoded list, so splitting a header (as
    ``XsiSimObj`` was split out of ``xsi_bfm.h`` so an edge model could compile without Vivado's
    headers) cannot leave the committed workspaces uncompilable while every gate stays green.
    """
    names = _framework_files()
    for f in sorted(ws.iterdir()):
        if not f.is_file() or f.name not in names:
            continue
        for inc in _INCLUDE.findall(_norm(f)):
            if inc not in names:
                continue            # a per-example generated header, not ours
            assert (ws / inc).is_file(), (
                f"{f.relative_to(_REPO)} includes framework header {inc!r}, which is missing from "
                f"{ws.relative_to(_REPO)}. The workspace cannot compile from a clean checkout.")
