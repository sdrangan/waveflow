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

Whether skills help is measured in ``evaluation/skill_tests/`` (docs: Evaluation > Agent skills).
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any

__all__ = ["SKILLS_DIR", "TARGETS", "list_skills", "install_skills"]

#: The shipped skills (package data).
SKILLS_DIR = Path(__file__).resolve().parents[1] / "skills"

#: Where each kind of agent looks for a project's skills.
TARGETS: dict[str, str] = {"claude": ".claude/skills", "agents": ".agents/skills"}

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
    installed, skipped = [], []
    for t in targets:
        for n in chosen:
            dst = root / TARGETS[t] / n
            if dst.exists() and not force:
                skipped.append(str(dst))
                continue
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(available[n], dst, ignore=shutil.ignore_patterns("__pycache__"))
            installed.append(str(dst))
    note = "already installed (pass --force to replace): " if skipped else ""
    return {"directory": str(root), "installed": installed, "skipped": skipped,
            "note": note + ", ".join(skipped) if skipped else ""}
