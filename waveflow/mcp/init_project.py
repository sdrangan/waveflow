"""``waveflow init``: set a project up so that any AI coding agent opened on it can use Waveflow.

One command, run in the project folder with the environment that has Waveflow installed, and safe
to re-run.  It writes two kinds of file:

**Committed with the project** (they hold nothing machine-specific, so a teammate's agent, or a
claude.ai/code session working from a clone of the project, gets them too):

- the agent skills, in ``.claude/skills/`` and ``.agents/skills/`` (:func:`install_skills`; a skill
  already there is left alone);
- ``AGENTS.md``, the instructions file Codex, Copilot, Gemini CLI and others read, with a Waveflow
  block of a few lines: the project uses Waveflow, load the ``waveflow-start`` skill, and how to
  install Waveflow when it is missing.  It is short on purpose: a project's ``AGENTS.md`` is usually
  its own (and other packages' too) and is read in every session; the detail is in the skill, which
  loads only when a task involves Waveflow;
- ``CLAUDE.md``, importing ``AGENTS.md`` (Claude Code reads ``CLAUDE.md``, not ``AGENTS.md``).

**Kept on this machine** (they name this machine's Python interpreter, so they are added to
``.gitignore`` when ``init`` creates them):

- ``.mcp.json`` (Claude Code) and ``.vscode/mcp.json`` (VS Code), registering the Waveflow MCP
  server.  An existing file is merged into, never replaced: only the ``waveflow`` entry changes.

The Waveflow block in ``AGENTS.md`` sits between two marker comments; re-running ``init`` replaces
that block (so it follows the installed Waveflow) and leaves the rest of the file alone.
"""
from __future__ import annotations

import json
import sys
from importlib import resources
from pathlib import Path
from typing import Any

from waveflow.mcp.skills import install_skills

__all__ = ["init_project", "BEGIN", "END"]

BEGIN = "<!-- waveflow:begin -- written by `waveflow init`; re-running it replaces this block -->"
END = "<!-- waveflow:end -->"
SERVER_ARGS = ["-m", "waveflow.mcp.server"]


def _block() -> str:
    text = resources.files("waveflow.mcp.resources").joinpath("agents_block.md").read_text(
        encoding="utf-8")
    return f"{BEGIN}\n{text.rstrip()}\n{END}\n"


def _write_agents_md(root: Path) -> str:
    path = root / "AGENTS.md"
    block = _block()
    if not path.exists():
        path.write_text("# Agent instructions\n\n" + block, encoding="utf-8")
        return "created"
    text = path.read_text(encoding="utf-8")
    start, end = text.find(BEGIN), text.find(END)
    if start >= 0 and end > start:
        new = text[:start] + block + text[end + len(END):].lstrip("\n")
        if new == text:
            return "unchanged"
        path.write_text(new, encoding="utf-8")
        return "updated"
    path.write_text(text.rstrip("\n") + "\n\n" + block, encoding="utf-8")
    return "appended"


def _write_claude_md(root: Path) -> str:
    path = root / "CLAUDE.md"
    if not path.exists():
        path.write_text("@AGENTS.md\n", encoding="utf-8")
        return "created"
    text = path.read_text(encoding="utf-8")
    if any(line.strip() == "@AGENTS.md" for line in text.splitlines()):
        return "unchanged"
    path.write_text(text.rstrip("\n") + "\n\n@AGENTS.md\n", encoding="utf-8")
    return "appended"


def _merge_server(path: Path, key: str, entry: dict[str, Any]) -> str:
    """Set ``config[key]["waveflow"] = entry`` in the JSON file *path*, keeping everything else."""
    existed = path.exists()
    config: dict[str, Any] = {}
    if existed:
        try:
            config = json.loads(path.read_text(encoding="utf-8") or "{}")
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path} is not valid JSON ({exc}); fix or remove it") from exc
    servers = config.setdefault(key, {})
    if servers.get("waveflow") == entry:
        return "unchanged"
    servers["waveflow"] = entry
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    return "updated" if existed else "created"


def _gitignore(root: Path, entries: list[str]) -> list[str]:
    path = root / ".gitignore"
    have = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    add = [e for e in entries if e not in have]
    if add:
        text = "\n".join(have).rstrip("\n")
        lines = ([text, ""] if text else []) + [
            "# The Waveflow MCP server config names this machine's Python (waveflow init)", *add]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return add


def init_project(
    directory: str | Path = ".",
    *,
    python: str | None = None,
    mcp: bool = True,
    force_skills: bool = False,
) -> dict[str, Any]:
    """Set *directory* up for AI coding agents; see the module docstring.

    *python* is the interpreter the MCP server runs with (default: the one running this).  *mcp*
    False skips the two server configs.  *force_skills* replaces installed skills (after upgrading
    Waveflow).
    """
    root = Path(directory).resolve()
    if not root.is_dir():
        return {"error": f"no such folder: {root}"}

    from waveflow.mcp.knowledge.roots import RootNotFound, repo_root

    try:
        checkout: Path | None = repo_root()
    except RootNotFound:
        checkout = None
    if checkout is not None and root == checkout:
        return {"error": f"{root} is the Waveflow checkout itself, which commits its own "
                         "AGENTS.md and skills; run `waveflow init` in your own project folder"}

    files: dict[str, str] = {}
    notes: list[str] = []
    py: str | None = None

    skills = install_skills(root, force=force_skills)
    if "error" in skills:
        return skills
    for p in skills["installed"]:
        files[Path(p).relative_to(root).as_posix()] = "installed"
    if skills["skipped"]:
        notes.append("skills already installed were left as they are; "
                     "`waveflow init --force-skills` replaces them (after upgrading Waveflow)")
    for p in skills["removed"]:
        files[Path(p).relative_to(root).as_posix()] = "removed (retired)"
    if skills["retired_kept"]:
        notes.append("retired skills you edited were left in place; delete them or merge your edits "
                     "into their replacements: " + ", ".join(
                         Path(p).relative_to(root).as_posix() for p in skills["retired_kept"]))

    files["AGENTS.md"] = _write_agents_md(root)
    files["CLAUDE.md"] = _write_claude_md(root)

    if mcp:
        from waveflow.scripts.waveflow_mcp_setup import validate_python_interpreter

        py = str(Path(python or sys.executable).resolve())
        try:
            validate_python_interpreter(py)
        except RuntimeError as exc:
            return {"error": str(exc), "directory": str(root), "files": files}
        try:
            claude = _merge_server(root / ".mcp.json", "mcpServers",
                                   {"type": "stdio", "command": py, "args": SERVER_ARGS})
            vscode = _merge_server(root / ".vscode" / "mcp.json", "servers",
                                   {"type": "stdio", "command": py, "args": SERVER_ARGS})
        except ValueError as exc:
            return {"error": str(exc), "directory": str(root), "files": files}
        files[".mcp.json"], files[".vscode/mcp.json"] = claude, vscode
        created = [f for f, s in ((".mcp.json", claude), (".vscode/mcp.json", vscode))
                   if s == "created"]
        if _gitignore(root, created):
            files[".gitignore"] = "updated"
        if claude != "unchanged":
            notes.append("Claude Code asks once whether to use the project's MCP servers: approve "
                         "`waveflow`.  If you also registered it with `claude mcp add`, remove "
                         "one of the two")

    if checkout is None:
        notes.append("this Waveflow install has no checkout (not `pip install -e`), so the MCP "
                     "search tools have no guide to read: clone https://github.com/sdrangan/waveflow "
                     "and `pip install -e` it, or set WAVEFLOW_KB_ROOT to a clone")
    return {"directory": str(root), "python": py,
            "files": files, "notes": notes,
            "next": "commit .claude/skills, .agents/skills, AGENTS.md and CLAUDE.md with the project"}
