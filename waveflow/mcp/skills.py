"""``waveflow skills``: the agent skills shipped with Waveflow, and installing them into a project.

A skill is a folder holding a ``SKILL.md`` -- a name, a one-line description, and short
instructions -- in the open Agent Skills format that Claude Code, Codex, Gemini CLI, Copilot and
others read.  An agent sees only the descriptions until a task matches one, then loads that skill.

Waveflow's skills live in ``waveflow/skills/`` and ship as package data, so the skills a project
installs match the Waveflow it runs.  Each agent looks for skills in its own folder, so
:func:`install_skills` copies them to every target asked for:

=========  ======================  ===========================================
target     folder                  read by
=========  ======================  ===========================================
claude     ``.claude/skills/``     Claude Code
agents     ``.agents/skills/``     the cross-agent location (Codex and others)
=========  ======================  ===========================================

A skill that has been renamed or withdrawn is *retired* (:data:`RETIRED`).  Installing its
replacement removes a project's copy of the retired skill -- otherwise an agent would load both --
but only a copy that is exactly a version Waveflow shipped; an edited copy is left and reported.

Whether skills help is measured in ``evaluation/skill_tests/`` (docs: Evaluation > Agent skills).
"""
from __future__ import annotations

import hashlib
import re
import shutil
from pathlib import Path
from typing import Any

__all__ = ["SKILLS_DIR", "TARGETS", "RETIRED", "list_skills", "install_skills"]

#: The shipped skills (package data).
SKILLS_DIR = Path(__file__).resolve().parents[1] / "skills"

#: Where each kind of agent looks for a project's skills.
TARGETS: dict[str, str] = {"claude": ".claude/skills", "agents": ".agents/skills"}

#: Skills Waveflow no longer ships, each with the skill that replaces it.
RETIRED: dict[str, str] = {
    "waveflow-module": "waveflow-kernel",
    "waveflow-hls-body": "waveflow-kernel-hls",
}

#: The SHA-256 (line endings normalized to LF) of every ``SKILL.md`` a retired skill shipped with.
#: A retired skill was one file, so a folder holding exactly that file, unchanged, is Waveflow's own
#: copy and safe to remove.
_RETIRED_SHA256: dict[str, frozenset[str]] = {
    "waveflow-module": frozenset({
        "73b93ef0e274cd53f5dc2f88b57a71cd5f8c0b8007922c049a857341695868a9",
        "7fa2190328c61250f28c19f4119b87b8291c6ab09b6b03e13b7548c125439889",
    }),
    "waveflow-hls-body": frozenset({
        "c5a807d161ad92044ee5bd7bcc8dcaa62a725cb2d03de9351a62413758ae8300",
        "ec794a65015d1a7afac695f2e325d60f9311f9680d8551757f64077f5cbd6990",
    }),
}

_FRONT = re.compile(r"\A---\s*\n(.*?)\n---", re.DOTALL)


def _front_matter(skill_md: Path) -> dict[str, str]:
    m = _FRONT.match(skill_md.read_text(encoding="utf-8"))
    out: dict[str, str] = {}
    if m:
        for line in m.group(1).splitlines():
            key, sep, val = line.partition(":")
            if sep and not line.startswith((" ", "\t")):
                out[key.strip()] = val.strip()
    return out


def _skill_md_sha256(skill_md: Path) -> str:
    text = skill_md.read_bytes().replace(b"\r\n", b"\n")
    return hashlib.sha256(text).hexdigest()


def _is_shipped_copy(folder: Path, name: str) -> bool:
    """True if *folder* is exactly a shipped version of the retired skill *name*: its one file,
    ``SKILL.md``, unchanged."""
    files = [p for p in folder.rglob("*") if p.is_file() and "__pycache__" not in p.parts]
    return ([p.relative_to(folder).as_posix() for p in files] == ["SKILL.md"]
            and _skill_md_sha256(folder / "SKILL.md") in _RETIRED_SHA256.get(name, frozenset()))


def _skill_folders(root: Path = SKILLS_DIR) -> list[Path]:
    return sorted(d for d in root.iterdir() if (d / "SKILL.md").is_file()) if root.is_dir() else []


def list_skills() -> dict[str, Any]:
    """Every shipped skill: its name, its description, and its files."""
    skills = []
    for d in _skill_folders():
        fm = _front_matter(d / "SKILL.md")
        skills.append({
            "name": fm.get("name", d.name),
            "description": fm.get("description", ""),
            "files": sorted(p.relative_to(d).as_posix() for p in d.rglob("*") if p.is_file()),
        })
    return {"skills_dir": str(SKILLS_DIR), "skills": skills}


def install_skills(
    directory: str | Path = ".",
    *,
    targets: list[str] | tuple[str, ...] = ("claude", "agents"),
    names: list[str] | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Copy the shipped skills into *directory*, once per target.

    *names* picks skills (default: all).  A skill already installed is left alone unless
    *force*, which replaces it -- so a project's edits to a skill are never overwritten silently.

    Installing a skill also removes the retired skill it replaces (:data:`RETIRED`) from the same
    target, when that copy is exactly one Waveflow shipped; an edited copy is kept and listed under
    ``retired_kept`` for the user to delete or merge.
    """
    bad = [t for t in targets if t not in TARGETS]
    if bad:
        return {"error": f"unknown target(s) {bad}; choose from {sorted(TARGETS)}"}
    available = {d.name: d for d in _skill_folders()}
    if not available:
        return {"error": f"no skills found in {SKILLS_DIR}"}
    chosen = names or sorted(available)
    missing = [n for n in chosen if n not in available]
    if missing:
        return {"error": f"no skill(s) named {missing}", "available": sorted(available)}

    root = Path(directory).resolve()
    installed, skipped, removed, retired_kept = [], [], [], []
    for t in targets:
        for old, new in RETIRED.items():
            dst = root / TARGETS[t] / old
            if new not in chosen or not dst.is_dir():
                continue
            if _is_shipped_copy(dst, old):
                shutil.rmtree(dst)
                removed.append(str(dst))
            else:
                retired_kept.append(str(dst))
        for n in chosen:
            dst = root / TARGETS[t] / n
            if dst.exists() and not force:
                skipped.append(str(dst))
                continue
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(available[n], dst, ignore=shutil.ignore_patterns("__pycache__"))
            installed.append(str(dst))
    notes = []
    if skipped:
        notes.append("already installed (pass --force to replace): " + ", ".join(skipped))
    if removed:
        notes.append("removed retired skills: " + ", ".join(removed))
    if retired_kept:
        notes.append("retired skills left in place because they were edited (delete them, or merge "
                     "your edits into " + ", ".join(sorted({RETIRED[Path(p).name] for p in retired_kept}))
                     + "): " + ", ".join(retired_kept))
    return {"directory": str(root), "installed": installed, "skipped": skipped,
            "removed": removed, "retired_kept": retired_kept, "note": "\n".join(notes)}
