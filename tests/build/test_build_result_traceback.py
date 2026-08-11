"""Tests for BuildResult.traceback — a failed step must keep more than str(exc)."""
from __future__ import annotations

from waveflow.build.build import BuildConfig, BuildDag, BuildStep


class _BoomStep(BuildStep):
    description = "Always raises."
    consumes = []
    produces = {}
    params = {}

    def run(self, config, **_):
        raise RuntimeError("kaboom")


class _FineStep(BuildStep):
    description = "Always succeeds."
    consumes = []
    produces = {}
    params = {}

    def run(self, config, **_):
        return {}


class TestBuildResultTraceback:
    def test_failure_keeps_traceback(self, tmp_path):
        dag = BuildDag()
        dag.add(_BoomStep(name="boom"))
        # force=True: a step that produces nothing is otherwise judged fresh and skipped.
        results = dag.run(BuildConfig(root_dir=tmp_path), force=True)

        result = results["boom"]
        assert result.success is False
        assert result.message == "kaboom"
        assert result.traceback is not None
        # The traceback must name the raising function, which is what str(exc) lacks.
        assert "RuntimeError: kaboom" in result.traceback
        assert "_BoomStep.run" in result.traceback or "in run" in result.traceback

    def test_success_has_no_traceback(self, tmp_path):
        dag = BuildDag()
        dag.add(_FineStep(name="fine"))
        results = dag.run(BuildConfig(root_dir=tmp_path), force=True)
        assert results["fine"].success is True
        assert results["fine"].traceback is None

    def test_default_is_none(self):
        from waveflow.build.build import BuildResult
        assert BuildResult(success=True).traceback is None
