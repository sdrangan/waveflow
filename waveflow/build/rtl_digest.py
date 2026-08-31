"""The stamp that records **which sources a synthesized design was built from**.

``<example>/<top>_proj/`` is gitignored build output, so nothing in a checkout says which sources
produced the RTL sitting in it.  :func:`~waveflow.build.trace_steps.rtl_staleness` used to answer
that with mtimes, and mtime is only a *proxy* for the question — one that is wrong in the two
directions that matter here:

* a ``--force`` regeneration rewrites ``gen/<top>.cpp`` with **identical bytes** and a new mtime, so
  proving the artifacts are byte-identical is what makes every gate skip;
* ``git checkout`` stamps a restored file with the checkout time, so a branch switch that restores
  *identical* content reads as stale too.

Both of those are silent: the gates skip, pytest prints ``40 passed, 23 skipped``, and a run that
measured nothing reads as success.  So the stamp records **content**: a SHA-256 per source file,
written beside the Vitis project at csynth time by
:func:`~waveflow.build.composite_gen.render_rtl_f`, and compared by ``rtl_staleness`` afterwards.

The source set is exactly the one the mtime check read — ``gen/<top>.cpp`` plus
``include/*.{h,hpp,cpp}`` — so behaviour changes *only* where mtime and content disagree.

.. warning::

   A **missing** stamp must fall back to the mtime check, never to "clean".  A build tree made
   before this module existed has no stamp, and turning it silently unguarded would trade a fix for
   silent skips against a new source of silent passes.  :func:`read_stamp` returns ``None`` for
   anything it cannot trust, and the caller falls back.

.. warning::

   Writing a stamp is only meaningful **immediately after csynth**.  A stamp written at any other
   moment records the sources as they are *now* against RTL built from something else, which is the
   guard disabling itself.  That is why ``render_rtl_f``'s ``stamp_sources`` argument exists and why
   the XSI gates — which re-render the ``.f`` from the RTL on disk — pass ``stamp_sources=False``.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

#: File name of the stamp, written inside ``<top>_proj/`` so it lives and dies with the RTL it
#: describes: deleting the project directory cannot leave a stamp behind to vouch for RTL that is
#: no longer there.
STAMP_NAME = "rtl_sources.json"

#: Schema version.  An unrecognized version reads as "no stamp" and falls back to mtimes, which is
#: the conservative direction: an unreadable stamp must never mean "clean".
STAMP_VERSION = 1

#: Extensions that count as a hand-written source under ``include/``.  Same set the mtime check
#: read, kept here so the stamp and the guard cannot drift apart.
SOURCE_SUFFIXES = (".h", ".hpp", ".cpp")


def source_files(root, top: str, *, gen_dir: str = "gen",
                 include_dir: str = "include") -> list[Path]:
    """The sources *top*'s RTL is built from, sorted, as absolute paths.

    Sorted so the stamp is byte-stable and so a mismatch report names the same file every run;
    ``iterdir`` order is a filesystem detail nobody should be able to observe through a gate.
    """
    root = Path(root)
    out: list[Path] = []
    gen = root / gen_dir / f"{top}.cpp"
    if gen.is_file():
        out.append(gen)
    inc = root / include_dir
    if inc.is_dir():
        out += [p for p in inc.iterdir() if p.is_file() and p.suffix in SOURCE_SUFFIXES]
    return sorted(out)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def source_digests(root, top: str, *, gen_dir: str = "gen",
                   include_dir: str = "include") -> dict[str, str]:
    """``{repo-relative posix path: sha256}`` for *top*'s source set.

    Keys are POSIX-spelled so a stamp written on Windows and read on Linux compares equal — the
    stamp lives in build output, but a shared build tree is not something to make surprising.
    """
    root = Path(root)
    return {p.relative_to(root).as_posix(): _sha256(p)
            for p in source_files(root, top, gen_dir=gen_dir, include_dir=include_dir)}


def stamp_path(root, top: str) -> Path:
    """Where *top*'s stamp lives: inside its Vitis project directory."""
    return Path(root) / f"{top}_proj" / STAMP_NAME


def write_stamp(root, top: str, *, gen_dir: str = "gen",
                include_dir: str = "include") -> Path | None:
    """Record the current source digests as the ones *top*'s RTL was built from.

    Returns the stamp path, or ``None`` if there is no project directory to write into (nothing was
    synthesized, so there is nothing to vouch for).

    **Call this only after csynth.**  See the module warning.
    """
    proj = Path(root) / f"{top}_proj"
    if not proj.is_dir():
        return None
    payload = {
        "version": STAMP_VERSION,
        "top": top,
        "files": source_digests(root, top, gen_dir=gen_dir, include_dir=include_dir),
    }
    out = proj / STAMP_NAME
    out.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return out


def read_stamp(root, top: str) -> dict[str, str] | None:
    """*top*'s recorded ``{path: sha256}``, or ``None`` if there is nothing trustworthy to read.

    ``None`` covers every unhappy case — absent, unparseable, wrong version, wrong shape — because
    the caller's response to all of them is the same and is the conservative one: fall back to the
    mtime check rather than declare the tree clean.
    """
    path = stamp_path(root, top)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("version") != STAMP_VERSION:
        return None
    files = data.get("files")
    if not isinstance(files, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in files.items()):
        return None
    return files


def first_mismatch(recorded: dict[str, str], current: dict[str, str]) -> tuple[str, str] | None:
    """``(path, verb)`` for the first source that disagrees, or ``None`` if they match.

    The verb ("has changed" / "is new" / "is gone") is what turns a digest comparison back into the
    sentence a human can act on; a bare hash pair tells nobody what to do.
    """
    for rel in sorted(set(recorded) | set(current)):
        was, now = recorded.get(rel), current.get(rel)
        if was == now:
            continue
        if was is None:
            return rel, "is new"
        if now is None:
            return rel, "is gone"
        return rel, "has changed"
    return None
