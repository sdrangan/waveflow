"""Locating the source trees the knowledge index is built from.

The index is built **in memory from the live checkout** -- there is no committed
copy to go stale.  That makes "where is the checkout?" the one piece of state
this package needs, and it is answered here so every module agrees.

Resolution order, first hit wins:

1. ``WAVEFLOW_KB_ROOT`` -- an explicit repository root.  The tests use it to
   point the index at a fixture tree.
2. walking up from this file until a directory holds both ``docs/guide`` and
   ``examples/`` -- true for an editable install (``pip install -e``), which is
   how the course venv and the lab machines install Waveflow.

``WAVEFLOW_KB_EXTRA_ROOTS`` (``os.pathsep``-separated) adds further trees, for
example a course's own examples.  Each is indexed the same way as the repo's
``examples/``.

A non-editable wheel has no ``docs/`` or ``examples/`` next to the package, so
:func:`repo_root` raises.  Shipping a build-time bundle for that case is
deliberately deferred; see ``plans/accel_mcp.md``.
"""
from __future__ import annotations

import os
from pathlib import Path

__all__ = ["repo_root", "extra_roots", "RootNotFound"]


class RootNotFound(RuntimeError):
    """No checkout could be located, so there is nothing to index."""


def _looks_like_root(p: Path) -> bool:
    return (p / "docs" / "guide").is_dir() and (p / "examples").is_dir()


def repo_root() -> Path:
    """Return the Waveflow checkout the index reads from."""
    env = os.environ.get("WAVEFLOW_KB_ROOT")
    if env:
        p = Path(env).expanduser().resolve()
        if not _looks_like_root(p):
            raise RootNotFound(
                f"WAVEFLOW_KB_ROOT={p} has no docs/guide and examples/ under it"
            )
        return p

    here = Path(__file__).resolve()
    for parent in here.parents:
        if _looks_like_root(parent):
            return parent
    raise RootNotFound(
        "no Waveflow checkout found above "
        f"{here} -- the knowledge index needs an editable install "
        "(pip install -e) or WAVEFLOW_KB_ROOT pointing at one"
    )


def extra_roots() -> list[Path]:
    """Extra example trees named by ``WAVEFLOW_KB_EXTRA_ROOTS``."""
    raw = os.environ.get("WAVEFLOW_KB_EXTRA_ROOTS", "")
    out: list[Path] = []
    for part in raw.split(os.pathsep):
        part = part.strip()
        if not part:
            continue
        p = Path(part).expanduser().resolve()
        if p.is_dir():
            out.append(p)
    return out
