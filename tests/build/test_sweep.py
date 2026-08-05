"""P1 of ``plans/sweep_runner.md`` — the grid a sweep visits.

The gate that matters is not that `ParamGrid` produces *a* product, but that it produces the **same
points in the same order** as the hand-written loops it replaces.  Order is not cosmetic here: with
`--resume`, a reordering silently changes which points an interrupted sweep still has to run, and the
two existing sweeps disagree about it — one iterates its first-named axis fastest, the other slowest.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from waveflow.build.sweep import ParamGrid, Stage, SweepRunner


class TestReproducesTheExistingSweeps:
    """Element for element, including order — the collapse must not move a single point."""

    def test_vecmult_sequence_is_unchanged(self):
        from examples.vecmult.vecmult_sweep import DWIDS, VLENS
        from examples.vecmult.vecmult_sweep import points as original

        grid = ParamGrid(vlen=VLENS, dwid=DWIDS)         # vlen outer, as the original iterates
        assert [dict(sorted(p.items())) for p in grid] == \
               [dict(sorted(p.items())) for p in original()]

    def test_fir_block_sequence_is_unchanged(self):
        from examples.fir_block.fir_block import DEFAULT_SAMP_I
        from examples.fir_block.fir_block_sweep import MEM_DWS, NTAPS, REALIZATIONS, SAMP_WS
        from examples.fir_block.fir_block_sweep import points as original

        grid = ParamGrid(ntap=NTAPS, samp_w=SAMP_WS, unroll_lane=REALIZATIONS,
                         mem_dwidth=MEM_DWS, samp_i=(DEFAULT_SAMP_I,))
        assert [dict(sorted(p.items())) for p in grid] == \
               [dict(sorted(p.items())) for p in original()]


class TestOrder:
    def test_declaration_order_is_iteration_order(self):
        """A grid should read the way it runs — the first axis named is the outer loop."""
        grid = ParamGrid(a=(1, 2), b=(10, 20))
        assert list(grid) == [{"a": 1, "b": 10}, {"a": 1, "b": 20},
                              {"a": 2, "b": 10}, {"a": 2, "b": 20}]

    def test_len_matches_what_iteration_yields(self):
        grid = ParamGrid(a=(1, 2, 3), b=(10, 20))
        assert len(grid) == len(list(grid)) == 6


class TestConstantsAndSubsets:
    def test_a_single_value_axis_is_a_constant(self):
        """No second concept for a held-fixed parameter: one value, no branching."""
        grid = ParamGrid(a=(1, 2), samp_i=(2,))
        assert len(grid) == 2
        assert all(p["samp_i"] == 2 for p in grid)

    def test_a_constant_does_not_pad_every_label(self):
        """It cannot distinguish points, so it would be noise in every log line and summary key."""
        grid = ParamGrid(a=(1, 2), samp_i=(2,))
        assert "samp_i" not in grid.label(next(iter(grid)))
        assert grid.label({"a": 1, "samp_i": 2}) == "a1"

    def test_labels_are_distinct_per_point(self):
        grid = ParamGrid(ntap=(8, 16), unroll=(False, True))
        assert len({grid.label(p) for p in grid}) == len(grid)

    def test_booleans_read_as_words(self):
        """`unroll_laneoff` is greppable in a log; `unroll_lane0` invites confusion with a count."""
        grid = ParamGrid(unroll=(False, True))
        assert {grid.label(p) for p in grid} == {"unrolloff", "unrollon"}

    def test_subset_restricts_one_axis_and_leaves_the_rest(self):
        grid = ParamGrid(ntap=(8, 16, 32), samp_w=(8, 12))
        narrowed = grid.subset(ntap=(16,))
        assert len(narrowed) == 2
        assert {p["ntap"] for p in narrowed} == {16}
        assert {p["samp_w"] for p in narrowed} == {8, 12}

    def test_subset_ignores_none_so_a_cli_need_not_special_case(self):
        grid = ParamGrid(ntap=(8, 16), samp_w=(8, 12))
        assert list(grid.subset(ntap=None, samp_w=None)) == list(grid)

    def test_subset_rejects_an_unknown_axis(self):
        """A typo in a flag should not silently sweep the full grid."""
        with pytest.raises(ValueError, match="not an axis"):
            ParamGrid(ntap=(8,)).subset(ntapp=(16,))

    def test_a_label_does_not_change_when_the_grid_is_narrowed(self):
        """Caught for real by running `--dry-run --vlen 512`, which labelled points `dwid64`.

        Narrowing an axis to one value makes it *look* like a constant, and a label recomputed from
        the narrowed grid drops it.  `--vlen 512 --resume` would then look for `dwid64` in a summary
        written as `vlen512_dwid64`, match nothing, and silently re-run every point it already had.
        """
        full = ParamGrid(vlen=(512, 1024), dwid=(32, 64))
        point = {"vlen": 512, "dwid": 64}
        assert full.subset(vlen=(512,)).label(point) == full.label(point) == "vlen512_dwid64"

    def test_narrowing_twice_still_agrees(self):
        full = ParamGrid(a=(1, 2), b=(3, 4))
        once = full.subset(a=(1,))
        assert once.subset(b=(3,)).label({"a": 1, "b": 3}) == full.label({"a": 1, "b": 3})


class TestBuildVersusWorkloadAxes:
    """The distinction that keeps a pysim sweep from re-synthesizing.

    A build axis is a `HwParam` and produces different hardware; a workload axis is a runtime input
    and only re-runs.  `ResourceModel.get_params` already drops `**runtime` for the same reason.
    """

    def test_axes_are_build_by_default(self):
        grid = ParamGrid(dwid=(32, 64))
        assert grid.build_axes and not grid.workload_axes

    def test_workload_axes_are_named_and_separated(self):
        grid = ParamGrid(dwid=(32, 64), nwords=(128, 512), _workload=("nwords",))
        assert set(grid.build_axes) == {"dwid"}
        assert set(grid.workload_axes) == {"nwords"}
        assert len(grid) == 4          # the product is over both, regardless

    def test_a_workload_name_that_is_not_an_axis_is_refused(self):
        with pytest.raises(ValueError, match="not axes"):
            ParamGrid(dwid=(32,), _workload=("nwords",))

    def test_subset_preserves_the_split(self):
        grid = ParamGrid(dwid=(32, 64), nwords=(128, 512), _workload=("nwords",))
        assert set(grid.subset(dwid=(32,)).workload_axes) == {"nwords"}


def test_an_empty_axis_is_refused():
    """It would yield no points at all — a whole sweep quietly doing nothing."""
    with pytest.raises(ValueError, match="no values"):
        ParamGrid(ntap=())


class TestStageForce:
    """What a stage forces, which is the difference between a build sweep and a workload sweep.

    DAG freshness is decided on file mtimes and knows nothing about params, so a build sweep must
    force or the second point silently reuses the first point's artifacts.  A workload sweep must
    NOT: the hardware is identical at every point, and forcing re-synthesizes for a design that
    cannot have changed.
    """

    def test_forcing_everything_is_the_default(self):
        """The safe answer, because the unsafe one mis-attributes a measurement rather than failing."""
        assert Stage("resources").force is True

    def test_a_stage_can_name_the_steps_a_point_reaches(self):
        stage = Stage("fit_timing", force=["pysim", "rtlsim"])
        assert stage.force == ["pysim", "rtlsim"]

    def test_the_runner_passes_it_through_verbatim(self):
        """The list reaches `BuildDag.run`, which is the only place it can mean anything."""
        seen = {}

        class _Dag:
            def run(self, config, *, through, force):
                seen["through"], seen["force"] = through, force
                return {}

        runner = SweepRunner(dag_factory=_Dag, root_dir=Path("."))
        runner.run_stage({"n_words": 128}, Stage("fit_timing", force=["rtlsim"]))
        assert seen == {"through": "fit_timing", "force": ["rtlsim"]}

    def test_a_bool_stays_a_bool(self):
        """`force=True` must not arrive as `['T','r','u','e']` -- a bool is not a sequence of names."""
        seen = {}

        class _Dag:
            def run(self, config, *, through, force):
                seen["force"] = force
                return {}

        runner = SweepRunner(dag_factory=_Dag, root_dir=Path("."))
        runner.run_stage({"n_words": 128}, Stage("pysim"))
        assert seen["force"] is True
