"""Tests for BuildConfig, BuildResult, BuildStep, Buildable, and BuildDag."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from waveflow.build.build import (
    Buildable,
    BuildConfig,
    BuildDag,
    BuildResult,
    BuildStep,
    FileArtifact,
    ObjectArtifact,
    SourceStep,
    source_artifact,
)


# ---------------------------------------------------------------------------
# BuildConfig
# ---------------------------------------------------------------------------

class TestBuildConfig:
    def test_defaults_root_dir_is_cwd(self):
        cfg = BuildConfig()
        assert cfg.root_dir == Path.cwd()

    def test_string_root_dir_converted_to_path(self):
        cfg = BuildConfig(root_dir="/tmp/out")
        assert isinstance(cfg.root_dir, Path)
        assert cfg.root_dir == Path("/tmp/out")

    def test_path_root_dir_accepted(self):
        p = Path("/tmp/out")
        cfg = BuildConfig(root_dir=p)
        assert cfg.root_dir == p

    def test_vitis_version_none_by_default(self):
        cfg = BuildConfig()
        assert cfg.vitis_version is None

    def test_explicit_fields(self):
        cfg = BuildConfig(
            root_dir="/proj",
            vitis_version="2025.1",
        )
        assert cfg.root_dir == Path("/proj")
        assert cfg.vitis_version == "2025.1"

    def test_vitis_version_tuple_none(self):
        assert BuildConfig().vitis_version_tuple() is None

    def test_vitis_version_tuple_parsed(self):
        cfg = BuildConfig(vitis_version="2025.1")
        assert cfg.vitis_version_tuple() == (2025, 1)

    def test_vitis_version_tuple_invalid_raises(self):
        cfg = BuildConfig(vitis_version="bad")
        with pytest.raises(ValueError, match="YYYY.M"):
            cfg.vitis_version_tuple()

    def test_needs_legacy_streamutils_cpp_none_version(self):
        assert BuildConfig().needs_legacy_streamutils_cpp() is True

    def test_needs_legacy_streamutils_cpp_old_version(self):
        assert BuildConfig(vitis_version="2024.2").needs_legacy_streamutils_cpp() is True

    def test_needs_legacy_streamutils_cpp_new_version(self):
        assert BuildConfig(vitis_version="2025.1").needs_legacy_streamutils_cpp() is False


# ---------------------------------------------------------------------------
# BuildResult
# ---------------------------------------------------------------------------

class TestBuildResult:
    def test_success_only(self):
        r = BuildResult(success=True)
        assert r.success is True
        assert r.message == ""
        assert r.artifacts == {}

    def test_failure_with_message(self):
        r = BuildResult(success=False, message="oops")
        assert r.success is False
        assert r.message == "oops"

    def test_artifacts_populated(self):
        p = Path("/out/foo.h")
        r = BuildResult(success=True, artifacts={"header": p})
        assert r.artifacts["header"] == p

    def test_artifacts_default_not_shared(self):
        r1 = BuildResult(success=True)
        r2 = BuildResult(success=True)
        r1.artifacts["x"] = Path("/x")
        assert "x" not in r2.artifacts


# ---------------------------------------------------------------------------
# BuildStep (concrete subclass for testing)
# ---------------------------------------------------------------------------

class TestBuildStep:
    def test_name_defaults_to_class_name(self):
        class MyStep(BuildStep):
            def run(self, config, **kwargs):
                return {}

        step = MyStep()
        assert step.name == "MyStep"

    def test_class_attributes_default_to_empty(self):
        class MyStep(BuildStep):
            def run(self, config, **kwargs):
                return {}

        step = MyStep()
        assert step.consumes == []
        assert step.produces == {}
        assert step.params == {}
        assert step.description == ""

    def test_subclass_overrides_class_attributes(self):
        class MyStep(BuildStep):
            description = "does something"
            consumes    = ["x"]
            produces    = {"y": None}
            params      = {"n": 5}

            def run(self, config, **kwargs):
                return {"y": kwargs["x"]}

        step = MyStep()
        assert step.description == "does something"
        assert step.consumes == ["x"]
        assert step.produces == {"y": None}
        assert step.params == {"n": 5}

    def test_abstract_run_prevents_direct_instantiation(self):
        with pytest.raises(TypeError):
            BuildStep()  # type: ignore[abstract]

    def test_run_raises_on_failure(self):
        class FailStep(BuildStep):
            def run(self, config, **kwargs):
                raise RuntimeError("validation failed")

        with pytest.raises(RuntimeError, match="validation failed"):
            FailStep().run(BuildConfig())


# ---------------------------------------------------------------------------
# Buildable (concrete subclass for testing)
# ---------------------------------------------------------------------------

class TestBuildable:
    def _make_buildable(self, outputs: dict[str, str]) -> Buildable:
        content_map = outputs

        class ConcreteBuildable(Buildable):
            @property
            def build_outputs(self):
                return {k: Path(k + ".txt") for k in content_map}

            def generate(self, key, config):
                return content_map[key]

        return ConcreteBuildable()

    def test_run_writes_files(self, tmp_path):
        b = self._make_buildable({"hello": "world content"})
        cfg = BuildConfig(root_dir=tmp_path)
        result = b.run(cfg)
        assert result.success is True
        out = tmp_path / "hello.txt"
        assert out.exists()
        assert out.read_text() == "world content"

    def test_run_returns_artifact_paths(self, tmp_path):
        b = self._make_buildable({"a": "aaa", "b": "bbb"})
        cfg = BuildConfig(root_dir=tmp_path)
        result = b.run(cfg)
        assert set(result.artifacts) == {"a", "b"}
        assert result.artifacts["a"] == tmp_path / "a.txt"

    def test_run_creates_parent_dirs(self, tmp_path):
        class NestedBuildable(Buildable):
            @property
            def build_outputs(self):
                return {"src": Path("subdir/nested/out.h")}

            def generate(self, key, config):
                return "// generated"

        cfg = BuildConfig(root_dir=tmp_path)
        result = NestedBuildable().run(cfg)
        assert result.success is True
        assert (tmp_path / "subdir" / "nested" / "out.h").exists()

    def test_run_captures_exception_as_failure(self, tmp_path):
        class BrokenBuildable(Buildable):
            @property
            def build_outputs(self):
                return {"bad": Path("bad.txt")}

            def generate(self, key, config):
                raise RuntimeError("generation error")

        result = BrokenBuildable().run(BuildConfig(root_dir=tmp_path))
        assert result.success is False
        assert "generation error" in result.message

    def test_is_a_build_step(self):
        b = self._make_buildable({"k": "v"})
        assert isinstance(b, BuildStep)

    def test_abstract_prevents_direct_instantiation(self):
        with pytest.raises(TypeError):
            Buildable()  # type: ignore[abstract]


# ---------------------------------------------------------------------------
# BuildDag
# ---------------------------------------------------------------------------

class TestBuildDag:
    def test_add_and_run_single_step(self, tmp_path):
        ran: list[str] = []

        class Alpha(BuildStep):
            def run(self, config, **kwargs):
                ran.append("Alpha")
                return {}

        dag = BuildDag()
        dag.add(Alpha())
        # force=True: Alpha produces no files, so without it the incremental-build
        # skip logic returns BuildResult(success=True, skipped=True) and `success`
        # passes without the step ever running.
        results = dag.run(BuildConfig(root_dir=tmp_path), force=True)
        assert results["Alpha"].success is True
        assert results["Alpha"].skipped is False
        assert ran == ["Alpha"]

    def test_run_executes_in_topological_order(self, tmp_path):
        order: list[str] = []

        class StepA(BuildStep):
            def run(self, config, **kwargs):
                order.append("A")
                return {}

        class StepB(BuildStep):
            def run(self, config, **kwargs):
                order.append("B")
                return {}

            def resolve_deps(self, other_steps):
                self._deps = [s for s in other_steps if isinstance(s, StepA)]

        dag = BuildDag()
        dag.add(StepA())
        dag.add(StepB())
        # These steps produce no files, so the incremental-build skip logic
        # would skip both.  force=True is what makes them actually execute;
        # this test is about ordering, not about skip logic.
        dag.run(BuildConfig(root_dir=tmp_path), force=True)
        assert order == ["A", "B"]

    def test_failure_propagates_to_dependents(self, tmp_path):
        ran_dependent: list[str] = []

        class FailStep(BuildStep):
            produces = {"x": None}

            def run(self, config, **kwargs):
                raise RuntimeError("upstream failed")

        class DependentStep(BuildStep):
            consumes = ["x"]

            def run(self, config, **kwargs):
                ran_dependent.append("ran")
                return {}

        dag = BuildDag()
        dag.add(FailStep())
        dag.add(DependentStep())
        results = dag.run(BuildConfig(root_dir=tmp_path), force=True)
        assert results["FailStep"].success is False
        assert "upstream failed" in results["FailStep"].message
        # run() halts the build at the first failure, so a step downstream of
        # a failure never executes and is never recorded (it used to be
        # recorded as success=False / "Skipped: dependency failed").
        assert ran_dependent == []
        assert "DependentStep" not in results

    def test_duplicate_name_raises(self):
        dag = BuildDag()

        class MyStep(BuildStep):
            def run(self, config):
                return BuildResult(success=True)

        dag.add(MyStep())
        with pytest.raises(ValueError, match="already exists"):
            dag.add(MyStep())

    def test_cycle_detection(self, tmp_path):
        dag = BuildDag()

        class StepA(BuildStep):
            def run(self, config):
                return BuildResult(success=True)

        class StepB(BuildStep):
            def run(self, config):
                return BuildResult(success=True)

        a = StepA()
        b = StepB()
        dag._steps = [a, b]
        dag._names = {"StepA", "StepB"}
        a._deps = [b]
        b._deps = [a]

        with pytest.raises(ValueError, match="cycle"):
            dag._topological_sort()

    def test_describe_shows_steps_and_deps(self, tmp_path):
        class StepA(BuildStep):
            def run(self, config):
                return BuildResult(success=True)

        class StepB(BuildStep):
            def run(self, config):
                return BuildResult(success=True)

            def resolve_deps(self, other_steps):
                self._deps = [s for s in other_steps if isinstance(s, StepA)]

        dag = BuildDag()
        dag.add(StepA())
        dag.add(StepB())
        desc = dag.describe()
        assert "StepA" in desc
        assert "StepB" in desc

    def test_add_returns_the_step(self, tmp_path):
        class MyStep(BuildStep):
            def run(self, config):
                return BuildResult(success=True)

        dag = BuildDag()
        step = MyStep()
        returned = dag.add(step)
        assert returned is step

    def test_artifacts_accessible_from_results(self, tmp_path):
        class WriteStep(Buildable):
            @property
            def build_outputs(self):
                return {"out": Path("result.txt")}

            def generate(self, key, config):
                return "content"

        dag = BuildDag()
        dag.add(WriteStep())
        results = dag.run(BuildConfig(root_dir=tmp_path))
        assert results["WriteStep"].success
        assert results["WriteStep"].artifacts["out"] == tmp_path / "result.txt"


# ---------------------------------------------------------------------------
# BuildArtifact hierarchy
# ---------------------------------------------------------------------------

class TestBuildArtifacts:
    def test_file_artifact_equals_path(self, tmp_path):
        p = tmp_path / "out.txt"
        p.write_text("x")
        fa = FileArtifact(path=p)
        assert fa == p

    def test_file_artifact_not_equal_different_path(self, tmp_path):
        p1 = tmp_path / "a.txt"
        p2 = tmp_path / "b.txt"
        p1.write_text("a")
        p2.write_text("b")
        assert FileArtifact(path=p1) != FileArtifact(path=p2)

    def test_file_artifact_delegates_exists(self, tmp_path):
        p = tmp_path / "f.txt"
        fa = FileArtifact(path=p)
        assert not fa.exists()
        p.write_text("hi")
        assert fa.exists()

    def test_file_artifact_freshness(self, tmp_path):
        import time as _time
        old = ObjectArtifact(value=None, timestamp=_time.time() - 10)
        p = tmp_path / "f.txt"
        p.write_text("x")
        fa = FileArtifact(path=p)
        # File was just written — its mtime should be newer than old.timestamp
        assert fa.is_fresh_relative_to(old)

    def test_source_artifact_uses_mtime(self, tmp_path):
        p = tmp_path / "src.txt"
        p.write_text("src")
        sa = source_artifact(p)
        assert isinstance(sa, FileArtifact)
        assert abs(sa.timestamp - p.stat().st_mtime) < 0.01

    def test_object_artifact_freshness(self):
        import time as _time
        old = ObjectArtifact(value="old", timestamp=_time.time() - 10)
        new = ObjectArtifact(value="new", timestamp=_time.time())
        assert new.is_fresh_relative_to(old)
        assert not old.is_fresh_relative_to(new)

    def test_build_result_path_accessor(self, tmp_path):
        p = tmp_path / "out.txt"
        p.write_text("x")
        r = BuildResult(success=True, artifacts={"f": p})
        assert r.path("f") == p

    def test_build_result_object_accessor(self):
        r = BuildResult(success=True, artifacts={"val": 42})
        assert r.object("val") == 42


# ---------------------------------------------------------------------------
# DAG artifact passing and advanced run() options
# ---------------------------------------------------------------------------

class TestBuildDagArtifacts:
    def test_step_receives_dep_artifact(self, tmp_path):
        received = {}

        class ProducerStep(BuildStep):
            produces = {"data": None}  # None = in-memory artifact

            def run(self, config, **kwargs):
                return {"data": 99}

        class ConsumerStep(BuildStep):
            consumes = ["data"]

            def run(self, config, data, **kwargs):
                received["val"] = data
                return {}

        dag = BuildDag()
        dag.add(ProducerStep())
        dag.add(ConsumerStep())
        dag.run(BuildConfig(root_dir=tmp_path), force=True)
        assert received["val"] == 99

    def test_param_injected_from_config(self, tmp_path):
        received = {}

        class MyStep(BuildStep):
            params = {"n": 10}

            def run(self, config, n, **kwargs):
                received["n"] = n
                return {}

        dag = BuildDag()
        dag.add(MyStep())
        dag.run(BuildConfig(root_dir=tmp_path, params={"n": 42}), force=True)
        assert received["n"] == 42

    def test_param_uses_default_when_absent_from_config(self, tmp_path):
        received = {}

        class MyStep(BuildStep):
            params = {"n": 10}

            def run(self, config, n, **kwargs):
                received["n"] = n
                return {}

        dag = BuildDag()
        dag.add(MyStep())
        dag.run(BuildConfig(root_dir=tmp_path), force=True)
        assert received["n"] == 10

    def test_consumes_produces_auto_wires_deps(self, tmp_path):
        order: list[str] = []

        class ProducerStep(BuildStep):
            produces = {"val": None}

            def run(self, config, **kwargs):
                order.append("producer")
                return {"val": 1}

        class ConsumerStep(BuildStep):
            consumes = ["val"]

            def run(self, config, val, **kwargs):
                order.append("consumer")
                return {}

        dag = BuildDag()
        dag.add(ProducerStep())
        dag.add(ConsumerStep())
        dag.run(BuildConfig(root_dir=tmp_path), force=True)
        assert order == ["producer", "consumer"]

    def test_exception_in_step_captured_as_failure(self, tmp_path):
        ran_next = []

        class BoomStep(BuildStep):
            def run(self, config, **kwargs):
                raise RuntimeError("boom")

        class NextStep(BuildStep):
            def run(self, config, **kwargs):
                ran_next.append("ran")
                return {}

        dag = BuildDag()
        dag.add(BoomStep())
        dag.add(NextStep())
        results = dag.run(BuildConfig(root_dir=tmp_path), force=True)
        # The exception is captured into a BuildResult rather than propagating
        # out of run().
        assert results["BoomStep"].success is False
        assert "boom" in results["BoomStep"].message
        # The first failure halts the whole build, so even a step with no
        # dependency on BoomStep does not run (it used to keep going).
        assert ran_next == []
        assert "NextStep" not in results

    def test_info_returns_new_format(self, tmp_path):
        class StepA(BuildStep):
            description = "does A"
            produces    = {"x": None}

            def run(self, config, **kwargs):
                return {"x": 1}

        class StepB(BuildStep):
            description = "does B"
            consumes    = ["x"]
            params      = {"k": 0}

            def run(self, config, **kwargs):
                return {}

        dag = BuildDag()
        dag.add(StepA())
        dag.add(StepB())
        info = dag.info()
        assert info[0]["step"] == "StepA"
        assert info[0]["description"] == "does A"
        assert info[0]["produces"] == ["x"]
        assert info[1]["consumes"] == ["x"]
        assert info[1]["params"] == {"k": 0}
        assert "optional" not in info[0]


# ---------------------------------------------------------------------------
# The late freshness hook (BuildStep.is_fresh; plans/system_dag.md Stage 1)
# ---------------------------------------------------------------------------

def _bump(path: Path, dt: float = 10.0) -> None:
    """Push *path*'s mtime *dt* seconds later, so the mtime rule calls it newer than its neighbours."""
    t = path.stat().st_mtime + dt
    os.utime(path, (t, t))


class _Gen(BuildStep):
    """Rewrites ``gen.txt`` from ``spec.txt`` -- the same bytes whenever the spec says the same."""
    consumes = ["spec"]
    produces = {"gen": Path("gen.txt")}
    calls = 0

    def run(self, config, spec, **kwargs):
        out = config.root_dir / "gen.txt"
        out.write_text(Path(spec).read_text().upper())
        self.calls += 1
        return {"gen": out}


class _Synth(BuildStep):
    """The csynth stand-in: records the digest of what it read as its output, and its hook says fresh
    while that digest still matches."""
    consumes = ["gen"]
    produces = {"rtl": Path("rtl.txt")}
    calls = 0
    asked = 0
    hook_enabled = True

    def run(self, config, gen, **kwargs):
        out = config.root_dir / "rtl.txt"
        out.write_text(hashlib.sha256(Path(gen).read_bytes()).hexdigest())
        self.calls += 1
        return {"rtl": out}

    def is_fresh(self, config, paths):
        if not self.hook_enabled:
            return None
        self.asked += 1
        rtl, gen = paths["rtl"], paths["gen"]
        if not rtl.is_file():
            return False
        return rtl.read_text() == hashlib.sha256(gen.read_bytes()).hexdigest()


class _Sim(BuildStep):
    """Downstream of the hooked step, on the plain mtime rule."""
    consumes = ["rtl"]
    produces = {"trace": Path("trace.txt")}
    calls = 0

    def run(self, config, rtl, **kwargs):
        out = config.root_dir / "trace.txt"
        out.write_text("ran " + Path(rtl).read_text())
        self.calls += 1
        return {"trace": out}


def _hook_dag(tmp_path, *, hook=True):
    (tmp_path / "spec.txt").write_text("abc")
    gen, synth, sim = _Gen(name="gen"), _Synth(name="synth"), _Sim(name="sim")
    synth.hook_enabled = hook
    dag = BuildDag()
    dag.add(SourceStep(artifact="spec", path=Path("spec.txt")))
    for s in (gen, synth, sim):
        dag.add(s)
    cfg = BuildConfig(root_dir=tmp_path)
    dag.run(cfg)                                          # the first build: everything runs
    assert (gen.calls, synth.calls, sim.calls) == (1, 1, 1)
    return dag, cfg, gen, synth, sim


class TestFreshnessHook:
    def test_identical_bytes_upstream_rewrite_skips_the_step(self, tmp_path):
        """The central case: the spec is touched, so gen runs and rewrites gen.txt with IDENTICAL
        bytes; the cascade marks synth, but its hook -- asked after gen ran -- says fresh."""
        dag, cfg, gen, synth, sim = _hook_dag(tmp_path)
        _bump(tmp_path / "spec.txt")
        res = dag.run(cfg)
        assert gen.calls == 2 and not res["gen"].skipped
        assert synth.calls == 1 and res["synth"].skipped and res["synth"].success
        assert res["synth"].artifacts["rtl"] == tmp_path / "rtl.txt"

    def test_the_cascade_stops_at_a_fresh_step(self, tmp_path):
        dag, cfg, gen, synth, sim = _hook_dag(tmp_path)
        _bump(tmp_path / "spec.txt")
        res = dag.run(cfg)
        assert sim.calls == 1 and res["sim"].skipped

    def test_a_changed_byte_runs_it_and_its_downstream(self, tmp_path):
        dag, cfg, gen, synth, sim = _hook_dag(tmp_path)
        (tmp_path / "spec.txt").write_text("abd")
        _bump(tmp_path / "spec.txt")
        res = dag.run(cfg)
        assert (gen.calls, synth.calls, sim.calls) == (2, 2, 2)
        assert not res["synth"].skipped and not res["sim"].skipped

    def test_the_hook_is_asked_after_upstream_ran(self, tmp_path):
        """A spec change reaches gen.txt only when gen runs: the hook must see the new content."""
        dag, cfg, gen, synth, sim = _hook_dag(tmp_path)
        (tmp_path / "spec.txt").write_text("xyz")
        _bump(tmp_path / "spec.txt")
        seen = []
        orig = synth.is_fresh
        synth.is_fresh = lambda config, paths: (seen.append(paths["gen"].read_text()),
                                                orig(config, paths))[1]
        dag.run(cfg)
        assert seen == ["XYZ"]

    def test_force_wins_over_the_hook(self, tmp_path):
        dag, cfg, gen, synth, sim = _hook_dag(tmp_path)
        dag.run(cfg, force=True)
        assert (gen.calls, synth.calls, sim.calls) == (2, 2, 2)
        res = dag.run(cfg, force=["synth"])
        assert synth.calls == 3 and not res["synth"].skipped
        assert gen.calls == 2                                # not forced, and fresh by mtime

    def test_a_forced_step_is_not_asked(self, tmp_path):
        dag, cfg, gen, synth, sim = _hook_dag(tmp_path)
        asked = synth.asked
        dag.run(cfg, force=["synth"])
        assert synth.asked == asked

    def test_hook_false_runs_a_step_the_mtime_rule_calls_fresh(self, tmp_path):
        """The content disagrees while the mtimes agree (an older file restored): the hook's False
        runs the step, and the cascade carries on from it."""
        dag, cfg, gen, synth, sim = _hook_dag(tmp_path)
        (tmp_path / "rtl.txt").write_text("not the digest")
        t = (tmp_path / "gen.txt").stat().st_mtime + 5
        os.utime(tmp_path / "rtl.txt", (t, t))
        _bump(tmp_path / "trace.txt", 20)
        res = dag.run(cfg)
        assert gen.calls == 1 and res["gen"].skipped
        assert synth.calls == 2 and not res["synth"].skipped
        assert sim.calls == 2 and not res["sim"].skipped

    def test_on_step_begin_reports_the_late_decision(self, tmp_path):
        dag, cfg, gen, synth, sim = _hook_dag(tmp_path)
        _bump(tmp_path / "spec.txt")
        will = {}
        dag.run(cfg, on_step_begin=lambda s, w, p: will.__setitem__(s.name, w))
        assert will == {"spec": False, "gen": True, "synth": False, "sim": False}

    def test_hook_none_keeps_the_mtime_rule_and_the_cascade(self, tmp_path):
        """No hook: the identical-bytes rewrite re-runs synth and sim, exactly as before the hook."""
        dag, cfg, gen, synth, sim = _hook_dag(tmp_path, hook=False)
        _bump(tmp_path / "spec.txt")
        res = dag.run(cfg)
        assert (gen.calls, synth.calls, sim.calls) == (2, 2, 2)
        assert not any(r.skipped for n, r in res.items() if n != "spec")

    def test_hook_none_matches_the_pre_run_answer(self, tmp_path):
        """With no hook answering, the late decision is the pre-run must-run set, step for step."""
        dag, cfg, gen, synth, sim = _hook_dag(tmp_path, hook=False)
        for touch in ("spec.txt", "gen.txt", "rtl.txt", "trace.txt"):
            _bump(tmp_path / touch, 30)
            pre = dag._determine_must_run(dag.steps(), dag.artifact_paths(cfg), set())
            will = {}
            dag.run(cfg, on_step_begin=lambda s, w, p: will.__setitem__(s.name, w))
            assert {n for n, w in will.items() if w} == pre, touch

    def test_status_honours_the_hook(self, tmp_path):
        dag, cfg, gen, synth, sim = _hook_dag(tmp_path)
        _bump(tmp_path / "gen.txt")                       # newer than rtl.txt, the same bytes
        by = {e["artifact"]: e for e in dag.results_status(cfg)}
        assert by["rtl"]["stale"] is False and by["rtl"]["hook"] is True
        assert by["trace"]["stale"] is False              # not stale through a fresh step
        (tmp_path / "gen.txt").write_text("CHANGED")
        by = {e["artifact"]: e for e in dag.results_status(cfg)}
        assert by["rtl"]["stale"] is True and by["rtl"]["hook"] is False
        assert by["trace"]["stale"] is True and by["trace"]["stale_because"] == ["rtl"]

    def test_status_without_a_hook_is_unchanged(self, tmp_path):
        dag, cfg, gen, synth, sim = _hook_dag(tmp_path, hook=False)
        _bump(tmp_path / "gen.txt")
        by = {e["artifact"]: e for e in dag.results_status(cfg)}
        assert by["rtl"]["stale"] is True and by["rtl"]["stale_because"] == ["gen"]
        assert by["rtl"]["hook"] is None
