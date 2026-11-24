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


def test_no_skill_points_into_the_evaluation_folder():
    """``evaluation/skill_tests/`` holds every skill test's reference answers.  A skill that points an
    agent there (the device skill once named a worked trial under ``evaluation/skill_tests/results/``)
    leads it to list the folder and see the answers' file names: two blind runs were discarded for it,
    2026-11-21.  Point to the guide and the curated examples instead."""
    for skill_md in sorted(sk.SKILLS_DIR.glob("*/SKILL.md")):
        assert "evaluation/" not in skill_md.read_text(encoding="utf-8"), skill_md


def test_every_skills_front_matter_is_valid_yaml():
    """An agent's loader parses the front matter as YAML; a value it cannot parse loses the skill its
    description -- the text that decides when the skill is loaded -- while ``_front_matter`` here,
    which splits lines, would read it fine.  So check YAML's own rule for an unquoted value (no ``: ``
    and no `` #`` inside, no leading indicator), and parse with PyYAML when it is installed.
    (waveflow-kernel and waveflow-device shipped with a ``: `` in the description, 2026-11-20.)"""
    try:
        import yaml
    except ImportError:                       # not a Waveflow dependency; the rule below still holds
        yaml = None
    for skill_md in sorted(sk.SKILLS_DIR.glob("*/SKILL.md")):
        m = sk._FRONT.match(skill_md.read_text(encoding="utf-8"))
        assert m, f"{skill_md}: no front matter"
        for line in m.group(1).splitlines():
            key, sep, val = line.partition(":")
            if not sep or line.startswith((" ", "\t")):
                continue
            val = val.strip()
            if val[:1] in "\"'":
                continue                      # quoted: YAML's escaping rules, not the plain-scalar ones
            assert ": " not in val and " #" not in val, f"{skill_md} `{key}`: quote it or reword it"
            assert val[:1] not in "[]{}&*!|>%@`" and not val.startswith(("- ", "? ")), (skill_md, key)
        if yaml is not None:
            front = yaml.safe_load(m.group(1))
            assert front["name"] == skill_md.parent.name, skill_md
            assert len(front["description"]) > 80, skill_md


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


def _retired_copy(tmp_path: Path, target: str, name: str) -> Path:
    """A project's copy of the retired skill *name* as Waveflow last shipped it (from git)."""
    text = subprocess.run(["git", "show", f"9c1af60:waveflow/skills/{name}/SKILL.md"], cwd=REPO,
                          capture_output=True, text=True, check=True).stdout
    d = tmp_path / sk.TARGETS[target] / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(text, encoding="utf-8", newline="\r\n")   # as git on Windows wrote it
    return d


def test_installing_removes_retired_skills_but_never_an_edited_one(tmp_path):
    """A project that installed ``waveflow-module`` before the rename must not keep it beside
    ``waveflow-kernel``: installing the replacement removes a shipped copy, and only that."""
    import pytest

    try:
        old = _retired_copy(tmp_path, "claude", "waveflow-module")
        edited = _retired_copy(tmp_path, "agents", "waveflow-hls-body")
    except (subprocess.CalledProcessError, FileNotFoundError):
        pytest.skip("no git history to take the retired skills from")
    (edited / "notes.md").write_text("my notes", encoding="utf-8")   # the project's own addition

    assert set(sk.RETIRED.values()) <= {s["name"] for s in sk.list_skills()["skills"]}
    assert not set(sk.RETIRED) & {s["name"] for s in sk.list_skills()["skills"]}

    out = sk.install_skills(tmp_path, names=["waveflow-schema"])        # not the replacement
    assert old.is_dir() and not out["removed"]

    out = sk.install_skills(tmp_path)
    assert not old.exists() and out["removed"] == [str(old)]
    assert edited.is_dir() and out["retired_kept"] == [str(edited)]
    assert "waveflow-kernel-hls" in out["note"] and str(edited) in out["note"]
    assert (tmp_path / ".claude" / "skills" / "waveflow-kernel" / "SKILL.md").is_file()

    (edited / "notes.md").unlink()                                       # back to the shipped file
    out = sk.install_skills(tmp_path, force=True)
    assert not edited.exists() and out["removed"] == [str(edited)]


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


def _tree(d: Path) -> dict[str, str]:
    """Every file under *d*, by relative path, line endings normalized (git may convert them)."""
    return {p.relative_to(d).as_posix(): p.read_text(encoding="utf-8").replace("\r\n", "\n")
            for p in sorted(d.rglob("*")) if p.is_file() and "__pycache__" not in p.parts}


def test_the_repos_committed_skills_match_the_shipped_ones():
    """The repo commits a copy of every shipped skill in ``.claude/skills/`` and ``.agents/skills/``,
    so an agent opened on the repo itself (Claude Code, claude.ai/code, Codex) is offered them.
    ``waveflow/skills/`` is the source; after editing a skill there, refresh the copies with
    ``waveflow skills install --dir . --force`` and commit them.  A copy of a skill that no longer
    ships fails too."""
    shipped = {s["name"] for s in sk.list_skills()["skills"]}
    for target in sk.TARGETS.values():
        copies = REPO / target
        found = {d.name for d in copies.glob("waveflow-*") if d.is_dir()}
        assert found == shipped, (
            f"{target}: {sorted(found)} != shipped {sorted(shipped)}; run "
            "`waveflow skills install --dir . --force` and commit (delete a retired skill's copy)")
        for name in shipped:
            assert _tree(copies / name) == _tree(sk.SKILLS_DIR / name), (
                f"{target}/{name} differs from waveflow/skills/{name}; run "
                "`waveflow skills install --dir . --force` and commit")


HLS_BODY = REPO / "evaluation" / "skill_tests" / "kernel_hls"


def test_the_hls_body_tests_reference_matches_the_golden(tmp_path):
    """The kernel-body test's given files generate their headers and pass their own check; the
    reference body, compiled as plain C++ by the reviewer's ``check_body.py`` (Vivado's MinGW + the
    Vitis headers), writes exactly the golden model's words, TLAST included, at both widths."""
    import importlib.util

    import pytest

    work = tmp_path / "given"
    work.mkdir()
    for f in (HLS_BODY / "given_01").glob("*.py"):
        shutil.copy2(f, work / f.name)
    for script in ("gen_headers.py", "gain_model.py"):
        r = subprocess.run([sys.executable, script], cwd=work, capture_output=True, text=True,
                           timeout=300)
        assert r.returncode == 0, f"{script}:\n{r.stdout}\n{r.stderr}"

    spec = importlib.util.spec_from_file_location("check_body", HLS_BODY / "check_body.py")
    cb = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cb)
    res = cb.check(HLS_BODY / "answers" / "ref_01", work=tmp_path / "check")
    if res.get("skipped"):
        pytest.skip(res["error"])
    assert res["compiled"], res["error"]
    for w in (32, 64):
        assert all(res["widths"][w].values()), (w, res["widths"][w])


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


MODULE = REPO / "evaluation" / "skill_tests" / "kernel"


def test_the_module_tests_reference_passes_and_the_checker_reads_it(tmp_path):
    """The module test's reference answer -- three blocks, each in its harness -- passes its own tests,
    and the reviewer's ``check_blocks.py`` finds every module free-running with stream ports only, and
    block B's register bank and queue."""
    work = tmp_path / "ref"
    shutil.copytree(MODULE / "answers" / "ref_01", work, ignore=shutil.ignore_patterns("__pycache__"))
    r = subprocess.run([sys.executable, str(MODULE / "check_blocks.py"), str(work)],
                       capture_output=True, text=True, timeout=1800)
    assert r.returncode == 0, r.stderr
    out = r.stdout
    assert "G1 test_blocks.py: PASS" in out, out
    for cls in ("ContrastStretch", "EnergyMonitor", "Correlator"):
        assert f"{cls}  [FreeRunMod]" in out, out
    assert "NOT A STREAM" not in out and "could not build" not in out, out
    assert "mm_views: RegBank(regs), QueueOut(q_report)" in out


DEVICE = REPO / "evaluation" / "skill_tests" / "device"


def test_the_device_tests_reference_passes(tmp_path):
    """The device test's reference answer -- a kernel between a memory reader and writer, behind a
    queue in and a status queue out, then on a bus with its host -- passes its own tests (pysim)."""
    work = tmp_path / "ref"
    shutil.copytree(DEVICE / "answers" / "ref_01", work, ignore=shutil.ignore_patterns("__pycache__"))
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                        str(work / "test_scale.py")], cwd=work, capture_output=True, text=True,
                       timeout=1800)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "6 passed" in r.stdout, r.stdout


PIPELINE = REPO / "evaluation" / "skill_tests" / "pipeline"


def test_the_pipeline_tests_reference_passes(tmp_path):
    """The pipeline test's reference -- a broadcast, a statistics stage, a detect stage -- passes its
    own tests (pysim), and the one recording what pysim cannot show stays an expected failure."""
    work = tmp_path / "ref"
    shutil.copytree(PIPELINE / "answers" / "ref_01", work, ignore=shutil.ignore_patterns("__pycache__"))
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                        str(work / "test_peaks.py")], cwd=work, capture_output=True, text=True,
                       timeout=1800)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "11 passed, 1 xfailed" in r.stdout, r.stdout


SYSTEM = REPO / "evaluation" / "skill_tests" / "system"


def test_the_system_tests_reference_passes_and_catches_both_broken_devices(tmp_path):
    """The system test's reference -- two given devices, a memory and a host on one bus, its DAG --
    passes its own tests (pysim, and the DAG through the steps that need no toolchain), and those
    tests fail against each broken variant the reviewer runs (a scaler that stops, one that drops a
    word): ``check_variants.py`` prints PASS, FAIL, FAIL."""
    work = tmp_path / "trial"
    shutil.copytree(SYSTEM / "given_01", work, ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(SYSTEM / "answers" / "ref_01", work, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("__pycache__", "*.md"))
    r = subprocess.run([sys.executable, str(SYSTEM / "check_variants.py"), str(work)],
                       capture_output=True, text=True, timeout=1800)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "variants caught: 2 of 2; given run passes" in r.stdout, r.stdout
    assert not (work / "_variants.py").exists()

