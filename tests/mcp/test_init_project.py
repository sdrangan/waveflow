"""``waveflow init``: one command sets a project up for AI coding agents, and is safe to re-run."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from waveflow.mcp.init_project import BEGIN, END, init_project

REPO = Path(__file__).resolve().parents[2]


def test_a_fresh_project_gets_skills_instructions_and_the_server(tmp_path):
    out = init_project(tmp_path)
    assert "error" not in out, out
    assert (tmp_path / ".claude" / "skills" / "waveflow-schema" / "SKILL.md").is_file()
    assert (tmp_path / ".agents" / "skills" / "waveflow-kernel-hls" / "SKILL.md").is_file()

    agents = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    assert BEGIN in agents and END in agents
    assert "git clone https://github.com/sdrangan/waveflow.git" in agents   # the cloud fallback
    assert (tmp_path / "CLAUDE.md").read_text(encoding="utf-8").strip() == "@AGENTS.md"

    py = str(Path(sys.executable).resolve())
    claude = json.loads((tmp_path / ".mcp.json").read_text(encoding="utf-8"))
    vscode = json.loads((tmp_path / ".vscode" / "mcp.json").read_text(encoding="utf-8"))
    assert claude["mcpServers"]["waveflow"]["command"] == py
    assert vscode["servers"]["waveflow"]["args"] == ["-m", "waveflow.mcp.server"]

    # the server configs name this machine's Python: kept out of the project's commits
    ignored = (tmp_path / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ".mcp.json" in ignored and ".vscode/mcp.json" in ignored


def test_rerunning_changes_nothing(tmp_path):
    init_project(tmp_path)
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    out = init_project(tmp_path)
    assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before
    for f in ("AGENTS.md", "CLAUDE.md", ".mcp.json", ".vscode/mcp.json"):
        assert out["files"][f] == "unchanged", f
    assert out["notes"]                      # says the skills were left as they are


def test_existing_files_are_merged_into_not_replaced(tmp_path):
    (tmp_path / "AGENTS.md").write_text("# Mine\n\nUse tabs.\n", encoding="utf-8")
    (tmp_path / "CLAUDE.md").write_text("Be brief.\n", encoding="utf-8")
    (tmp_path / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"other": {"command": "x"}}}), encoding="utf-8")
    (tmp_path / ".gitignore").write_text("build/\n", encoding="utf-8")

    out = init_project(tmp_path)
    agents = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    assert agents.startswith("# Mine\n\nUse tabs.\n") and BEGIN in agents
    assert (tmp_path / "CLAUDE.md").read_text(encoding="utf-8").startswith("Be brief.\n")
    servers = json.loads((tmp_path / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]
    assert set(servers) == {"other", "waveflow"}
    assert out["files"][".mcp.json"] == "updated"
    # the project already had .mcp.json, maybe committed on purpose: only the new file is ignored
    ignored = (tmp_path / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ignored[0] == "build/" and ".vscode/mcp.json" in ignored and ".mcp.json" not in ignored

    # a stale block is replaced in place; the user's text around it stays
    stale = agents.replace("## Waveflow", "## Waveflow (old)") + "\nAfter.\n"
    (tmp_path / "AGENTS.md").write_text(stale, encoding="utf-8")
    assert init_project(tmp_path)["files"]["AGENTS.md"] == "updated"
    now = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    assert "(old)" not in now and now.count(BEGIN) == 1 and now.rstrip().endswith("After.")


def test_no_mcp_and_bad_json(tmp_path):
    out = init_project(tmp_path, mcp=False)
    assert not (tmp_path / ".mcp.json").exists() and out["python"] is None
    (tmp_path / ".mcp.json").write_text("{not json", encoding="utf-8")
    assert "not valid JSON" in init_project(tmp_path)["error"]


def test_the_waveflow_checkout_itself_is_refused():
    assert "checkout itself" in init_project(REPO)["error"]


def test_the_cli(tmp_path, capsys):
    from waveflow.cli import main

    assert main(["init", "--dir", str(tmp_path), "--no-mcp", "--text"]) == 0
    text = capsys.readouterr().out
    assert "AGENTS.md" in text and "next: commit" in text


def test_the_block_is_short_and_names_the_start_skill():
    """Projects usually have an AGENTS.md of their own, maybe with other packages' parts: ours is a
    few lines, and the detail lives in the ``waveflow-start`` skill, loaded only when needed."""
    from importlib import resources

    from waveflow.mcp.skills import list_skills

    block = resources.files("waveflow.mcp.resources").joinpath("agents_block.md").read_text(
        encoding="utf-8")
    assert len(block.splitlines()) <= 10
    assert "`waveflow-start`" in block
    assert "waveflow-start" in [s["name"] for s in list_skills()["skills"]]


def test_every_tool_and_command_the_start_skill_names_exists():
    """The start skill names MCP tools, CLI commands and guide pages; each must be real."""
    import asyncio
    import re

    from waveflow.cli import build_parser
    from waveflow.mcp.knowledge import waveflow_get_doc
    from waveflow.mcp.server import mcp
    from waveflow.mcp.skills import SKILLS_DIR

    block = (SKILLS_DIR / "waveflow-start" / "SKILL.md").read_text(encoding="utf-8")
    tools = {t.name for t in asyncio.run(mcp.list_tools())}
    for name in set(re.findall(r"`(waveflow_\w+)`", block)):
        assert name in tools, name
    for page in set(re.findall(r"`([\w/]+\.md)`", block)):
        assert "error" not in waveflow_get_doc(page), page
    parser = build_parser()
    kb_cmds = set(re.findall(r"`waveflow kb --text (\w+)", block))
    assert kb_cmds == {"search", "browse", "doc", "examples", "example", "usage"}, kb_cmds
    for cmd in kb_cmds:   # the argument each takes, if any
        arg = {"search": ["q"], "doc": ["systems/index.md"], "example": ["x"], "usage": ["X"]}
        parser.parse_args(["kb", "--text", cmd, *arg.get(cmd, [])])   # SystemExit if unknown
    parser.parse_args(["frames", "--text"])
    parser.parse_args(["process", "--text"])
