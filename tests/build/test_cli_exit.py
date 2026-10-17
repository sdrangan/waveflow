"""run_dag_cli: a failed step fails the command (exit status 1); a green build returns."""
import sys

import pytest

from waveflow.build.build import BuildDag, BuildStep
from waveflow.build.cli import run_dag_cli


class Ok(BuildStep):
    produces = {"a": None}

    def run(self, config, **kwargs):
        return {"a": 1}


class Boom(BuildStep):
    consumes = ["a"]
    produces = {"b": None}

    def run(self, config, **kwargs):
        raise RuntimeError("boom")


def _cli(dag, tmp_path, monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", ["build.py", *argv])
    return run_dag_cli(lambda: dag, description="t", default_through=None, root_dir=tmp_path)


def test_a_failed_step_exits_1(tmp_path, monkeypatch, capsys):
    dag = BuildDag()
    dag.add(Ok(name="ok"))
    dag.add(Boom(name="boom"))
    with pytest.raises(SystemExit) as e:
        _cli(dag, tmp_path, monkeypatch, "--force")
    assert e.value.code == 1
    assert "FAILED: boom" in capsys.readouterr().out


def test_a_green_build_returns(tmp_path, monkeypatch):
    dag = BuildDag()
    dag.add(Ok(name="ok"))
    assert _cli(dag, tmp_path, monkeypatch, "--force") is not None
