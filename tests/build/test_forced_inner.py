"""Forcing ``csynth`` re-synthesizes every top: the forced outer step forces its inner DAG, instead of
asking each inner step whether it is fresh (and skipping them all)."""
from pathlib import Path

import waveflow.build.system_dag as system_dag
from waveflow.build.build import BuildConfig, BuildDag, SourceStep
from waveflow.build.system_dag import CsynthStep, CsynthTopsStep


def _run(tmp_path, monkeypatch, force):
    ran = []
    monkeypatch.setattr(system_dag, "rtl_problem", lambda root, top: None)      # every top fresh
    monkeypatch.setattr(CsynthStep, "run", lambda self, config, **_: ran.append(self.top) or {
        k: config.root_dir / v for k, v in system_dag._csynth_products(self.top).items()})
    for d in ("include", "gen"):
        (tmp_path / d).mkdir()
    dag = BuildDag()
    dag.add(SourceStep(artifact="include", path=Path("include")))
    dag.add(SourceStep(artifact="gen", path=Path("gen")))
    dag.add(CsynthTopsStep(tops=("a", "b")))
    res = dag.run(BuildConfig(root_dir=tmp_path), force=force)
    assert all(r.success for r in res.values()), {n: r.message for n, r in res.items()}
    return ran


def test_a_forced_csynth_rebuilds_every_top(tmp_path, monkeypatch):
    assert _run(tmp_path, monkeypatch, ["csynth"]) == ["a", "b"]


def test_a_fresh_csynth_rebuilds_nothing(tmp_path, monkeypatch):
    assert _run(tmp_path, monkeypatch, False) == []
