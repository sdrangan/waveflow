"""P2 of ``plans/sweep_runner.md`` — the runner's behaviour, against a fake DAG.

None of what makes a sweep runner worth having needs a toolchain: failure isolation, incremental
save, per-stage resume and the stage list are all decisions about *control flow*.  Testing them
against a stub means they are covered on every run rather than only when someone spends 12 minutes of
Vitis, which is the difference between a guarantee and a hope.

Each of these encodes a lesson one of the three hand-written sweeps learned separately — and one
(`calibrate_platform.py`) never learned at all.
"""
from __future__ import annotations

import json

import pytest

from waveflow.build.sweep import ParamGrid, Stage, SweepRunner


class _FakeResult:
    def __init__(self, success=True, message=""):
        self.success = success
        self.message = message
        self.skipped = False
        self.elapsed_seconds = 0.0


class _FakeDag:
    """Records every (params, through) it was run with; fails or raises on demand."""

    calls: list = []

    def __init__(self, *, fail_when=None, raise_when=None):
        self.fail_when = fail_when
        self.raise_when = raise_when

    def run(self, config, through=None, force=False):
        _FakeDag.calls.append((dict(config.params), through))
        if self.raise_when and self.raise_when(config.params):
            raise RuntimeError("synthesis exploded")
        if self.fail_when and self.fail_when(config.params):
            return {"csynth": _FakeResult(False, "II not met")}
        return {"codegen": _FakeResult(), "csynth": _FakeResult()}


@pytest.fixture(autouse=True)
def _reset():
    _FakeDag.calls = []


def _runner(tmp_path, **kw):
    factory = kw.pop("factory", lambda: _FakeDag())
    return SweepRunner(dag_factory=factory, root_dir=tmp_path,
                       summary=tmp_path / "results" / "sweep.json", **kw)


class TestEveryPointRuns:
    def test_one_call_per_point_in_grid_order(self, tmp_path):
        grid = ParamGrid(a=(1, 2), b=(10, 20))
        res = _runner(tmp_path).run(grid, Stage("resources"), verbose=False)
        assert [c[0]["a"] for c in _FakeDag.calls] == [1, 1, 2, 2]
        assert len(res.points) == 4 and res.ok and res.complete

    def test_extra_params_ride_along_without_being_swept(self, tmp_path):
        """`live_output=False` is not a design point; it should not multiply the grid."""
        grid = ParamGrid(a=(1, 2))
        _runner(tmp_path, extra_params={"live_output": False}).run(
            grid, Stage("resources"), verbose=False)
        assert all(c[0]["live_output"] is False for c in _FakeDag.calls)
        assert len(_FakeDag.calls) == 2


class TestAFailingPointIsData:
    """"A point that blows up is data, not a stop" — what all three sweeps chose independently."""

    def test_a_failed_step_does_not_stop_the_sweep(self, tmp_path):
        grid = ParamGrid(a=(1, 2, 3))
        r = _runner(tmp_path, factory=lambda: _FakeDag(fail_when=lambda p: p["a"] == 2))
        res = r.run(grid, Stage("resources"), verbose=False)
        assert len(_FakeDag.calls) == 3, "the sweep stopped early"
        assert len(res.failures) == 1
        assert "II not met" in res.failures[0][2]

    def test_an_exception_becomes_a_record_rather_than_propagating(self, tmp_path):
        grid = ParamGrid(a=(1, 2, 3))
        r = _runner(tmp_path, factory=lambda: _FakeDag(raise_when=lambda p: p["a"] == 2))
        res = r.run(grid, Stage("resources"), verbose=False)          # must not raise
        assert len(_FakeDag.calls) == 3
        assert "RuntimeError: synthesis exploded" in res.failures[0][2]

    def test_the_run_reports_not_ok_so_a_cli_can_exit_nonzero(self, tmp_path):
        r = _runner(tmp_path, factory=lambda: _FakeDag(fail_when=lambda p: True))
        assert not r.run(ParamGrid(a=(1,)), Stage("resources"), verbose=False).ok


class TestIncrementalSave:
    """The lesson `vecmult_sweep.py` wrote into a docstring and the other two did not have."""

    def test_the_summary_is_written_after_every_point(self, tmp_path):
        seen = []
        summary = tmp_path / "results" / "sweep.json"

        class _Watching(_FakeDag):
            def run(self, config, through=None, force=False):
                seen.append(json.loads(summary.read_text()) if summary.exists() else None)
                return super().run(config, through, force)

        _runner(tmp_path, factory=_Watching).run(ParamGrid(a=(1, 2, 3)), Stage("resources"),
                                                 verbose=False)
        counts = [0 if s is None else s["n_points"] for s in seen]
        assert counts == [0, 1, 2], f"summary not written between points: {counts}"

    def test_a_partial_file_says_so(self, tmp_path):
        """So a truncated run cannot be mistaken for a whole one — a stale summary reading as fresh.

        `complete` is about *coverage*, not success: a sweep that attempted every point is complete
        even if some failed, because the failures are recorded.  What must never read as complete is a
        run that stopped early, so the mid-sweep writes carry `complete: false`.
        """
        summary = tmp_path / "results" / "sweep.json"
        seen = []

        class _Watching(_FakeDag):
            def run(self, config, through=None, force=False):
                if summary.exists():
                    seen.append(json.loads(summary.read_text())["complete"])
                return super().run(config, through, force)

        _runner(tmp_path, factory=_Watching).run(ParamGrid(a=(1, 2, 3)), Stage("resources"),
                                                 verbose=False)
        assert seen and not any(seen), "a mid-sweep summary claimed to be complete"
        assert json.loads(summary.read_text())["complete"] is True

    def test_every_point_attempted_counts_as_complete_even_with_failures(self, tmp_path):
        r = _runner(tmp_path, factory=lambda: _FakeDag(fail_when=lambda p: p["a"] == 2))
        res = r.run(ParamGrid(a=(1, 2, 3)), Stage("resources"), verbose=False)
        assert res.complete and not res.ok


class TestStages:
    """A point may need more than one run — the shape a timing sweep needs."""

    def test_each_stage_runs_once_per_point(self, tmp_path):
        res = _runner(tmp_path).run(ParamGrid(a=(1, 2)),
                                    [Stage("pysim"), Stage("resources")], verbose=False)
        assert [c[1] for c in _FakeDag.calls] == ["pysim", "resources", "pysim", "resources"]
        assert set(res.points["a1"]) == {"pysim", "resources"}

    def test_a_stage_can_skip_a_point(self, tmp_path):
        """The RTL-subset case: expensive side over a subset, no special mode."""
        stages = [Stage("pysim"), Stage("rtl", when=lambda p: p["a"] == 1)]
        res = _runner(tmp_path).run(ParamGrid(a=(1, 2)), stages, verbose=False)
        assert [c[1] for c in _FakeDag.calls] == ["pysim", "rtl", "pysim"]
        assert set(res.points["a2"]) == {"pysim"}

    def test_a_dry_run_stage_attaches_no_platform(self, tmp_path):
        """Nothing was synthesized, so there is no report to file."""
        captured = []

        class _Cap(_FakeDag):
            def run(self, config, through=None, force=False):
                captured.append(config.platform)
                return super().run(config, through, force)

        r = _runner(tmp_path, factory=_Cap, platform="work_plat",
                    platforms_root=tmp_path / "calib" / "work")
        r.run(ParamGrid(a=(1,)), [Stage("codegen", use_platform=False), Stage("resources")],
              verbose=False)
        assert captured == [None, "work_plat"]


class TestResume:
    def test_resume_skips_pairs_already_ok(self, tmp_path):
        grid = ParamGrid(a=(1, 2, 3))
        r = _runner(tmp_path, factory=lambda: _FakeDag(fail_when=lambda p: p["a"] == 2))
        r.run(grid, Stage("resources"), verbose=False)
        assert len(_FakeDag.calls) == 3

        _FakeDag.calls = []
        r2 = _runner(tmp_path)                       # everything succeeds this time
        r2.run(grid, Stage("resources"), resume=True, verbose=False)
        assert [c[0]["a"] for c in _FakeDag.calls] == [2], "resume re-ran points already ok"

    def test_resume_is_per_stage_not_per_point(self, tmp_path):
        """So a change to the cheap side does not re-run the expensive one.

        This is the property that makes a two-cadence timing sweep affordable: pysim over the whole
        grid on every edit, RTL only when the hardware changed.
        """
        grid = ParamGrid(a=(1,))
        r = _runner(tmp_path, factory=lambda: _FakeDag(fail_when=lambda p: False))
        r.run(grid, [Stage("pysim"), Stage("rtl")], verbose=False)

        # forget only the cheap stage, as a re-run after a pysim-side change would
        summary = tmp_path / "results" / "sweep.json"
        blob = json.loads(summary.read_text())
        blob["points"]["a1"].pop("pysim")
        summary.write_text(json.dumps(blob))

        _FakeDag.calls = []
        _runner(tmp_path).run(grid, [Stage("pysim"), Stage("rtl")], resume=True, verbose=False)
        assert [c[1] for c in _FakeDag.calls] == ["pysim"], "resume re-ran the expensive stage"


class TestTheSummaryIsALog:
    def test_it_records_no_counters(self, tmp_path):
        """The numbers live in the record store; a second copy is one that can go stale.

        Nothing read the old summary's `top` / `module_sum` / `integration` blobs, and since the
        integration term is now filed as a record all three are in the store.
        """
        _runner(tmp_path).run(ParamGrid(a=(1,)), Stage("resources"), verbose=False)
        blob = json.loads((tmp_path / "results" / "sweep.json").read_text())
        rec = blob["points"]["a1"]["resources"]
        assert set(rec) <= {"ok", "elapsed", "error", "filed"}
        assert not ({"top", "module_sum", "integration"} & set(rec))

    def test_it_records_the_grid_it_covered(self, tmp_path):
        """A log that cannot say what space it covered is harder to read than one that repeats itself."""
        blob_grid = _runner(tmp_path).run(ParamGrid(a=(1, 2), b=(3,)), Stage("s"),
                                          verbose=False).to_json()["grid"]
        assert blob_grid == {"a": [1, 2], "b": [3]}
