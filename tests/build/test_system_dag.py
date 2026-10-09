"""``waveflow/build/system_dag.py`` -- the fast half (``plans/system_dag.md``).

The RTL half is the example gates (``tests/examples/test_markov_xsi.py``, ``test_mm_fir_xsi.py``).
Here: csynth's freshness and its two modes, with a stand-in for Vitis.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from waveflow.build.build import BuildConfig, BuildDag, BuildStep
from waveflow.build.system_dag import (
    CsynthStep,
    CsynthTopsStep,
    rtl_problem,
    rtl_rel,
)


# ---------------------------------------------------------------------------------------------------
# csynth (Stage 2), with run_vitis_hls replaced by a stand-in that writes a .v and says OK
# ---------------------------------------------------------------------------------------------------

@pytest.fixture
def fake_vitis(monkeypatch):
    calls: list[str] = []

    class _R:
        stdout, stderr = "WAVEFLOW_CSYNTH_OK\n", ""

    def run_vitis_hls(tcl, work_dir=None, **_):
        top = Path(tcl).stem
        d = Path(work_dir) / rtl_rel(top)
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{top}.v").write_text(f"module {top}; endmodule\n", encoding="utf-8")
        calls.append(top)
        return _R()

    monkeypatch.setattr("waveflow.toolchain.toolchain.run_vitis_hls", run_vitis_hls)
    return calls


class _Codegen(BuildStep):
    """Writes ``gen/<top>.cpp`` for each top and one shared header -- the same bytes every run unless
    ``edits`` says otherwise."""

    produces = {"include": Path("include"), "gen": Path("gen")}
    tops = ("a", "b")
    edits: dict = {}

    def is_fresh(self, config, paths):
        return False

    def run(self, config, **_):
        root = Path(config.root_dir)
        (root / "include").mkdir(exist_ok=True)
        (root / "gen").mkdir(exist_ok=True)
        (root / "include" / "common.h").write_text(self.edits.get("common.h", "// common\n"))
        for t in self.tops:
            (root / "gen" / f"{t}.cpp").write_text(self.edits.get(f"{t}.cpp", f"// top {t}\n"))
            (root / "gen" / f"{t}.tcl").write_text("# tcl\n")
        return {"include": root / "include", "gen": root / "gen"}


def _csynth_dag():
    dag = BuildDag()
    cg = _Codegen(name="codegen")
    cg.edits = {}
    dag.add(cg)
    cs = CsynthTopsStep(name="csynth", tops=("a", "b"))
    dag.add(cs)
    return dag, cg, cs


def test_csynth_builds_a_fresh_tree_then_skips_it(tmp_path, fake_vitis):
    dag, cg, cs = _csynth_dag()
    cfg = BuildConfig(root_dir=tmp_path)
    res = dag.run(cfg)
    assert res["csynth"].success and fake_vitis == ["a", "b"]
    assert all(rtl_problem(tmp_path, t) is None for t in ("a", "b"))
    res = dag.run(cfg)                       # codegen rewrites identical bytes: nothing to synthesize
    assert not res["codegen"].skipped and res["csynth"].skipped
    assert fake_vitis == ["a", "b"]


def test_csynth_reruns_only_the_top_whose_source_changed(tmp_path, fake_vitis):
    dag, cg, cs = _csynth_dag()
    cfg = BuildConfig(root_dir=tmp_path)
    dag.run(cfg)
    cg.edits = {"b.cpp": "// top b, edited\n"}
    res = dag.run(cfg)
    assert res["csynth"].success and fake_vitis == ["a", "b", "b"]
    assert cs.last_run == {"csynth_a": "fresh", "csynth_b": "ran"}


def test_a_shared_header_change_reruns_every_top(tmp_path, fake_vitis):
    """The stamp hashes all of include/ (the plan's open question, 'narrower stamps')."""
    dag, cg, cs = _csynth_dag()
    cfg = BuildConfig(root_dir=tmp_path)
    dag.run(cfg)
    cg.edits = {"common.h": "// common, edited\n"}
    dag.run(cfg)
    assert fake_vitis == ["a", "b", "a", "b"]


def test_check_mode_fails_on_a_stale_top_and_never_synthesizes(tmp_path, fake_vitis):
    dag, cg, cs = _csynth_dag()
    dag.run(BuildConfig(root_dir=tmp_path))
    cg.edits = {"a.cpp": "// top a, edited\n"}
    res = dag.run(BuildConfig(root_dir=tmp_path, params={"synth": "check"}))
    assert not res["csynth"].success
    assert "a:" in res["csynth"].message and "gen/a.cpp has changed" in res["csynth"].message
    assert fake_vitis == ["a", "b"]


def test_check_mode_fails_on_a_missing_top(tmp_path, fake_vitis):
    dag, cg, cs = _csynth_dag()
    res = dag.run(BuildConfig(root_dir=tmp_path, params={"synth": "check"}))
    assert not res["csynth"].success and "no csynth RTL for a" in res["csynth"].message
    assert fake_vitis == []


def test_check_mode_passes_a_fresh_tree(tmp_path, fake_vitis):
    dag, cg, cs = _csynth_dag()
    dag.run(BuildConfig(root_dir=tmp_path))
    res = dag.run(BuildConfig(root_dir=tmp_path, params={"synth": "check"}))
    assert res["csynth"].success and fake_vitis == ["a", "b"]


def test_a_missing_stamp_falls_back_to_mtime_never_to_clean(tmp_path, fake_vitis):
    dag, cg, cs = _csynth_dag()
    cfg = BuildConfig(root_dir=tmp_path)
    dag.run(cfg)
    (tmp_path / "a_proj" / "rtl_sources.json").unlink()
    cg.edits = {"a.cpp": "// edited, and newer than the RTL\n"}
    dag.steps()[0].run(cfg)
    t = time.time() + 10
    os.utime(tmp_path / "gen" / "a.cpp", (t, t))
    assert "no source stamp" in rtl_problem(tmp_path, "a")
    assert cs.is_fresh(cfg, {}) is False


def test_status_reads_a_stamped_top_fresh_when_gen_is_newer(tmp_path, fake_vitis):
    dag, cg, cs = _csynth_dag()
    cfg = BuildConfig(root_dir=tmp_path)
    dag.run(cfg)
    dag.steps()[0].run(cfg)                  # gen/ rewritten, same bytes, newer
    by = {e["artifact"]: e for e in dag.results_status(cfg)}
    assert by["rtl_a"]["stale"] is False and by["rtl_b"]["stale"] is False


def test_bad_synth_mode_is_refused(tmp_path, fake_vitis):
    dag, cg, cs = _csynth_dag()
    res = dag.run(BuildConfig(root_dir=tmp_path, params={"synth": "maybe"}))
    assert not res["csynth"].success and "synth must be one of" in res["csynth"].message


def test_csynth_step_writes_the_stamp(tmp_path, fake_vitis):
    (tmp_path / "gen").mkdir()
    (tmp_path / "gen" / "t.cpp").write_text("// t\n")
    (tmp_path / "gen" / "t.tcl").write_text("# tcl\n")
    step = CsynthStep(top="t")
    assert step.name == "csynth_t"
    assert step.is_fresh(BuildConfig(root_dir=tmp_path), {}) is False
    step.run(BuildConfig(root_dir=tmp_path))
    assert (tmp_path / "t_proj" / "rtl_sources.json").is_file()
    assert step.is_fresh(BuildConfig(root_dir=tmp_path), {}) is True


def test_a_failed_csynth_reports_the_vitis_log(tmp_path, monkeypatch):
    import subprocess

    def run_vitis_hls(tcl, work_dir=None, **_):
        raise subprocess.CalledProcessError(1, "vitis-run", output="WAVEFLOW_ERROR: csynth\nboom\n")

    monkeypatch.setattr("waveflow.toolchain.toolchain.run_vitis_hls", run_vitis_hls)
    dag, cg, cs = _csynth_dag()
    res = dag.run(BuildConfig(root_dir=tmp_path))
    assert not res["csynth"].success
    assert "csynth of a failed" in res["csynth"].message and "boom" in res["csynth"].message
