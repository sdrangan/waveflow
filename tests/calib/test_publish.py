"""tests/calib/test_publish.py — promoting calibration from the work dir into the tracked library.

The two-tier storage contract (waveflow/calib/publish.py): sweeps write a churny work dir; a single
`publish_calib` command promotes the STABLE artifacts (params + distilled points/corpus) into the
tracked platform library.  This checks the properties the split exists to guarantee:

* only the stable artifacts are published (raw rtl/ pysim/ firing trees are excluded);
* an unchanged re-publish is a NO-OP (byte-identical -> not rewritten -> no git churn);
* dry-run writes nothing; --apply writes only the changed files;
* the coverage-regression guard refuses to replace a fit with a thinner one unless forced.
"""
from __future__ import annotations

import json

import pytest

from waveflow.calib.publish import (
    RegressionError,
    apply_plan,
    build_plan,
    main,
)


def _make_work(root, *, bus_points=2, comp="mem_w_stream_task", corpus_rows=2, params="{}"):
    """A work platform dir with a bus fit (mm_bus.json + N point files), one component (params.json +
    an N-row corpus.csv), and a raw pysim/ firing tree that must NOT be published."""
    (root / "mm_bus.json").parent.mkdir(parents=True, exist_ok=True)
    (root / "mm_bus.json").write_text(json.dumps({"models": {"write": {}}}))
    pts = root / "points"
    pts.mkdir(parents=True, exist_ok=True)
    for i in range(bus_points):
        (pts / f"n{i}.json").write_text(json.dumps({"write": {"num_trans": i, "nwords": 16 * i}}))
    cdir = root / "components" / comp
    cdir.mkdir(parents=True, exist_ok=True)
    (cdir / "params.json").write_text(params)
    header = "nwords,num_trans,residual\n"
    (cdir / "corpus.csv").write_text(header + "".join(f"{16*i},{i},{i}\n" for i in range(corpus_rows)))
    # the churn that must stay behind:
    raw = cdir / "pysim" / "n0"
    raw.mkdir(parents=True, exist_ok=True)
    (raw / "firings.csv").write_text("nwords,span\n128,161\n")
    return root


class TestScope:
    def test_publishes_stable_artifacts_not_raw_trees(self, tmp_path):
        work = _make_work(tmp_path / "work")
        tracked = tmp_path / "tracked"
        apply_plan(build_plan(work, tracked))

        assert (tracked / "mm_bus.json").is_file()
        assert (tracked / "points" / "n0.json").is_file()
        assert (tracked / "components" / "mem_w_stream_task" / "params.json").is_file()
        assert (tracked / "components" / "mem_w_stream_task" / "corpus.csv").is_file()
        # the raw firing tree is left behind
        assert not (tracked / "components" / "mem_w_stream_task" / "pysim").exists()


class TestNoOp:
    def test_unchanged_republish_writes_nothing(self, tmp_path):
        work = _make_work(tmp_path / "work")
        tracked = tmp_path / "tracked"
        apply_plan(build_plan(work, tracked))

        # a second publish of the identical work dir: every action is "unchanged", nothing written.
        plan2 = build_plan(work, tracked)
        assert plan2.changed == []
        assert apply_plan(plan2) == []

    def test_only_the_changed_file_is_written(self, tmp_path):
        work = _make_work(tmp_path / "work")
        tracked = tmp_path / "tracked"
        apply_plan(build_plan(work, tracked))

        # change just the params; the bus + corpus are byte-identical.
        (work / "components" / "mem_w_stream_task" / "params.json").write_text('{"nwords": 1.5}')
        plan = build_plan(work, tracked)
        assert [a.rel for a in plan.changed] == ["components/mem_w_stream_task/params.json"]
        assert plan.changed[0].status == "updated"
        assert apply_plan(plan) == ["components/mem_w_stream_task/params.json"]


class TestRegressionGuard:
    def test_thinner_refit_is_refused(self, tmp_path):
        work = _make_work(tmp_path / "work", bus_points=3, corpus_rows=3)
        tracked = tmp_path / "tracked"
        apply_plan(build_plan(work, tracked))                      # tracked has 3 points / 3 rows

        thin = _make_work(tmp_path / "work2", bus_points=2, corpus_rows=2, params='{"x": 1}')
        plan = build_plan(thin, tracked)
        assert {g.unit for g in plan.regressions} == {"bus", "components/mem_w_stream_task"}
        with pytest.raises(RegressionError, match="coverage regression"):
            apply_plan(plan)

    def test_force_publishes_the_regression(self, tmp_path):
        work = _make_work(tmp_path / "work", bus_points=3)
        tracked = tmp_path / "tracked"
        apply_plan(build_plan(work, tracked))
        thin = _make_work(tmp_path / "work2", bus_points=2, params='{"x": 1}')
        assert apply_plan(build_plan(thin, tracked), force=True)     # no raise, writes

    def test_first_publish_has_no_regression(self, tmp_path):
        """An empty tracked dir (tracked count 0) is never a regression."""
        plan = build_plan(_make_work(tmp_path / "work"), tmp_path / "fresh")
        assert plan.regressions == []


class TestCLI:
    def test_dry_run_writes_nothing(self, tmp_path, capsys):
        work = _make_work(tmp_path / "work")
        tracked = tmp_path / "tracked"
        rc = main([str(work), str(tracked)])
        assert rc == 0
        assert not tracked.exists()
        assert "dry-run" in capsys.readouterr().out

    def test_apply_writes(self, tmp_path):
        work = _make_work(tmp_path / "work")
        tracked = tmp_path / "tracked"
        assert main([str(work), str(tracked), "--apply"]) == 0
        assert (tracked / "mm_bus.json").is_file()

    def test_apply_refuses_regression_nonzero(self, tmp_path, capsys):
        work = _make_work(tmp_path / "work", bus_points=3)
        tracked = tmp_path / "tracked"
        main([str(work), str(tracked), "--apply"])
        thin = _make_work(tmp_path / "work2", bus_points=2)
        rc = main([str(thin), str(tracked), "--apply"])
        assert rc == 1
        assert "REFUSED" in capsys.readouterr().out
