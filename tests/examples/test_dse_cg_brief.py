"""examples/dse_cg's brief: the committed DSE.md is the generator's, and the arms differ in pysim only
(plans/dse_cg.md step 3c and *Fairness rules* 1).  No toolchain: the generator builds the Python
system and generates the HLS (seconds a configuration), synthesizes nothing."""

from __future__ import annotations

import difflib
from pathlib import Path

import pytest

from examples.dse_cg import brief
from examples.dse_cg.ltbf_cg_mem import LtbfCgMemAdaptor

HERE = Path(brief.__file__).resolve().parent


@pytest.fixture(scope="module")
def blocks():
    return brief.build_blocks(LtbfCgMemAdaptor(), HERE)


def test_the_committed_dse_md_is_what_the_generator_writes(blocks):
    want = brief.render(brief.CONTEXT_TEMPLATE.read_text(encoding="utf-8"), blocks, "waveflow")
    got = (HERE / "DSE.md").read_text(encoding="utf-8")
    diff = "".join(difflib.unified_diff(got.splitlines(True), want.splitlines(True), "committed",
                                        "generated", n=1))
    assert got == want, ("examples/dse_cg/DSE.md is stale: regenerate it with "
                         "brief.write_context(LtbfCgMemAdaptor(), Path('examples/dse_cg/DSE.md'))\n"
                         + diff[:3000])


def test_the_arms_differ_only_in_their_pysim_blocks(tmp_path):
    """Each source's arm blocks are all ``arm:waveflow`` and all about pysim; the traditional arm is
    the same text without them, and that is what install_skill writes."""
    sources = {"SKILL.md": brief.SKILL / "SKILL.md", "dselib/__init__.py": brief.SKILL / "dselib" / "__init__.py",
               "DSE.md.tmpl": brief.CONTEXT_TEMPLATE}  # fmt: skip
    for name, path in sources.items():
        src = path.read_text(encoding="utf-8")
        found = brief._ARM_BLOCK.findall(src)
        assert found and all(arm == "waveflow" and "pysim" in body for arm, body in found), name
        assert brief._ARM_BLOCK.sub("", src) == brief.select_arm(src, "traditional"), name
    wf, tr = brief.install_skill(tmp_path / "wf"), brief.install_skill(tmp_path / "tr", "traditional")
    for rel in ("SKILL.md", "dselib/__init__.py"):
        src = (brief.SKILL / rel).read_text(encoding="utf-8")
        for out, arm in ((wf, "waveflow"), (tr, "traditional")):
            want = brief.select_arm(src, arm)
            want = brief._COMMENT.sub("", want) if rel.endswith(".md") else want
            assert (out / rel).read_text(encoding="utf-8") == want, (rel, arm)


def test_the_two_sandboxes_differ_only_in_the_waveflow_blocks(tmp_path):
    """build_sandbox for each arm: the same files but for DSE.md and the skill, and those differ by
    exactly the arm:waveflow blocks; every link of DSE.md resolves inside the sandbox; nothing of the
    repository's own instructions (CLAUDE.md) is there (plan step 5, *Fairness rules* 1)."""
    import re

    design = LtbfCgMemAdaptor()
    wf = brief.build_sandbox(design, "waveflow", tmp_path / "wf")
    tr = brief.build_sandbox(design, "traditional", tmp_path / "tr")
    files = {a: {p.relative_to(d).as_posix(): p.read_bytes() for p in d.rglob("*") if p.is_file()}
             for a, d in (("waveflow", wf), ("traditional", tr))}
    assert files["waveflow"].keys() == files["traditional"].keys()
    differ = {f for f in files["waveflow"] if files["waveflow"][f] != files["traditional"][f]}
    assert differ == {"DSE.md", ".claude/skills/dse/SKILL.md", ".claude/skills/dse/dselib/__init__.py"}
    tmpl = brief.CONTEXT_TEMPLATE.read_text(encoding="utf-8")
    blocks = brief.build_blocks(design, brief.REPO)
    assert files["traditional"]["DSE.md"].decode() == brief.render(tmpl, blocks, "traditional")
    assert "pysim" in files["waveflow"]["DSE.md"].decode()
    assert not any(f.endswith("CLAUDE.md") for f in files["waveflow"])
    for d in (wf, tr):
        text = (d / "DSE.md").read_text(encoding="utf-8")
        for target in re.findall(r"\]\(([^)#\s]+)", text):
            assert (d / target).is_file(), target
