"""The shipped agent skills (``waveflow/skills``), ``waveflow skills``, and the skill tests'
reference answer (``evaluation/skill_tests``).

A skill points the agent at guide pages and names APIs; both go stale when the guide or the API
changes, and nothing else would notice.  So: every page a skill names must exist in the knowledge
index, and the reference answer to the schema skill's test must still run.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

from waveflow.mcp import skills as sk

REPO = Path(__file__).resolve().parents[2]
REF = REPO / "evaluation" / "skill_tests" / "schema" / "answers" / "ref_01"


def test_every_shipped_skill_has_a_name_and_a_description():
    listed = sk.list_skills()["skills"]
    assert "waveflow-schema" in [s["name"] for s in listed]
    for s in listed:
        assert s["name"] and len(s["description"]) > 80, s["name"]   # the description is the trigger
        assert (sk.SKILLS_DIR / s["name"] / "SKILL.md").is_file()     # name == folder name


def test_every_guide_page_a_skill_names_exists():
    from waveflow.mcp.knowledge import waveflow_get_doc

    for s in sk.list_skills()["skills"]:
        text = (sk.SKILLS_DIR / s["name"] / "SKILL.md").read_text(encoding="utf-8")
        pages = set(re.findall(r"`([\w/]+\.md)`", text)) - {"SKILL.md"}
        assert pages, s["name"]
        for page in pages:
            assert "error" not in waveflow_get_doc(page), f"{s['name']} names {page}"


def test_install_copies_to_each_target_and_keeps_local_edits(tmp_path):
    out = sk.install_skills(tmp_path)
    assert (tmp_path / ".claude" / "skills" / "waveflow-schema" / "SKILL.md").is_file()
    assert (tmp_path / ".agents" / "skills" / "waveflow-schema" / "SKILL.md").is_file()
    assert not out["skipped"]

    edited = tmp_path / ".claude" / "skills" / "waveflow-schema" / "SKILL.md"
    edited.write_text("mine", encoding="utf-8")
    out = sk.install_skills(tmp_path, targets=["claude"])
    assert out["skipped"] and edited.read_text(encoding="utf-8") == "mine"
    sk.install_skills(tmp_path, targets=["claude"], force=True)
    assert edited.read_text(encoding="utf-8") != "mine"

    assert "unknown target" in sk.install_skills(tmp_path, targets=["vim"])["error"]
    assert "no skill" in sk.install_skills(tmp_path, names=["nope"])["error"]


def test_the_cli_lists_and_installs(tmp_path, capsys):
    from waveflow.cli import main

    assert main(["skills", "list", "--text"]) == 0
    assert "waveflow-schema" in capsys.readouterr().out
    assert main(["skills", "install", "--dir", str(tmp_path), "--for", "agents", "--text"]) == 0
    assert (tmp_path / ".agents" / "skills" / "waveflow-schema").is_dir()
    assert not (tmp_path / ".claude").exists()


def test_the_schema_tests_reference_answer_runs(tmp_path):
    """The reference answer generates its headers -- the array utils in include/ with the rest --
    and its model passes the worked example at both widths."""
    work = tmp_path / "ref"
    shutil.copytree(REF, work, ignore=shutil.ignore_patterns("include", "__pycache__", "*.h"))
    for script in ("gen_headers.py", "pulse_model.py"):
        r = subprocess.run([sys.executable, script], cwd=work, capture_output=True, text=True,
                           timeout=300)
        assert r.returncode == 0, f"{script}:\n{r.stdout}\n{r.stderr}"
    assert r.stdout.strip().endswith("ok")
    inc = work / "include"
    for h in ("pulse_cmd.h", "peak.h", "fixed12_1_array_utils.h", "streamutils_hls.h"):
        assert (inc / h).is_file(), h
    assert not list(work.glob("*_array_utils*.h"))       # none left in the root


def test_the_lint_flags_hand_packing_and_spares_generated_headers(tmp_path):
    lint = REPO / "evaluation" / "skill_tests" / "lint_schema_use.py"
    (tmp_path / "include").mkdir()
    (tmp_path / "include" / "gen.h").write_text("x = w.range(11, 0);\n", encoding="utf-8")
    (tmp_path / "body.cpp").write_text(
        "void f(hls::stream<streamutils::axi4s_word<W>>& s) {\n"   # a template, not a shift
        "  a = s.read().data.range(11, 0);\n"
        "  if (cmd.op == 1) return;\n}\n", encoding="utf-8")
    (tmp_path / "model.py").write_text("n = int(cmd.n)\nhi = (w >> 16) & 0xFFFF\n", encoding="utf-8")
    r = subprocess.run([sys.executable, str(lint), str(tmp_path)], capture_output=True, text=True)
    hits = [line for line in r.stdout.splitlines() if ": [" in line]
    assert not [h for h in hits if "gen.h" in h]
    assert any("body.cpp:2: [range]" in h for h in hits)
    assert any("body.cpp:3: [enum as int]" in h for h in hits)
    assert not any("body.cpp:1:" in h for h in hits)
    assert any("model.py:1: [int(field)]" in h for h in hits)
    assert any("model.py:2: [shift]" in h for h in hits) and any("[mask]" in h for h in hits)
