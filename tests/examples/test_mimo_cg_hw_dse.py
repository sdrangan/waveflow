"""Step 6.2 of plans/mimo_cg/mimo_cg_paper_sims.md: the design-space exploration in Python.

The joint space has 6,084,720 designs; every scenario has a design within each loss budget at a
width the hardware can build; the frontier is exactly the non-dominated set; a decision picks the
cheapest design that meets a job-time budget; and the committed tables are what the code
regenerates.  No toolchain is involved.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from examples.mimo_cg.hw import dse, space
from examples.mimo_cg.mimo_cg import read_table


@pytest.fixture(scope="module")
def acc():
    return dse.accuracy()


@pytest.fixture(scope="module")
def hw():
    return dse.hw_table()


def _reference_pareto(points: np.ndarray) -> np.ndarray:
    """The definition, pair by pair: dropped when another row is no worse everywhere and better
    somewhere, or equal everywhere and earlier."""
    keep = np.ones(len(points), dtype=bool)
    for i, p in enumerate(points):
        for j, q in enumerate(points):
            if j != i and np.all(q <= p) and (np.any(q < p) or j < i):
                keep[i] = False
                break
    return keep


# --- the two sides ---------------------------------------------------------------------------


def test_accuracy_rows_are_the_ones_the_hardware_can_build(acc):
    assert len(dse.scenarios(acc)) == 27
    assert set(acc.W) == set(space.SPACE_W) and set(acc.g_s) == set(space.SPACE_G)
    assert {
        K: tuple(sorted(acc[acc.K == K].nit.unique())) for K in (4, 8, 16)
    } == dse.NITS
    # one row per scenario, format and iteration count
    assert not acc.duplicated(["modulation", "M", "K", "W", "g_s", "nit"]).any()
    assert len(acc) == 9 * 15 * sum(len(n) for n in dse.NITS.values())
    assert np.isinf(acc.loss[~acc.ok]).all() and np.isfinite(acc.loss[acc.ok]).all()


def test_the_joint_space_has_6_million_designs(hw, acc):
    assert len(hw) == 107_460
    assert dse.joint_designs(hw, acc) == dse.JOINT_DESIGNS == 6_084_720


def test_memory_floor():
    # K² + K·N complex values, one per 32-bit word, two per 64-bit word
    assert dse.memory_floor(16, 32) == 16 * 16 + 16 * 32 == 768
    assert dse.memory_floor(16, 64) == 384
    assert dse.memory_floor(4, 32) == 144


# --- candidates ------------------------------------------------------------------------------


def test_every_scenario_has_a_design_within_every_budget(hw, acc):
    for scn in dse.scenarios(acc):
        for budget in dse.LOSS_BUDGETS_DB:
            assert len(dse.reach(acc, scn, budget)) >= 1, (scn, budget)


def test_candidates_run_the_fewest_iterations_that_reach_the_budget(hw, acc):
    scn, budget = ("16qam", 64, 8), 0.5
    cands = dse.candidates(hw, acc, scn, budget)
    a = acc[(acc.modulation == scn[0]) & (acc.M == scn[1]) & (acc.K == scn[2])]
    for (W, g), grp in cands.groupby(["W", "g_s"]):
        rows = a[(a.W == W) & (a.g_s == g) & a.ok & (a.loss <= budget)]
        assert set(grp.nit) == {rows.nit.min()}
        assert set(grp.loss) == {rows[rows.nit == rows.nit.min()].loss.iloc[0]}
    # every configuration of the scenario's K whose format reaches the budget, in space order
    formats = set(zip(cands.W, cands.g_s, strict=True))
    in_reach = np.array([(W, g) in formats for W, g in zip(hw.W, hw.g_s, strict=True)])
    want = hw[(hw.K == 8).to_numpy() & in_reach]
    assert list(cands.order) == list(want.order)
    assert np.allclose(cands.job, cands.t0 + cands.nit * cands.t_iter)
    assert ((cands.mem_ratio < dse.GUARD) == cands.guarded).all()
    assert ((cands.loss + 2 * cands.sigma > budget) == cands.fragile).all()


def test_a_looser_budget_never_needs_more_iterations(hw, acc):
    for scn in dse.scenarios(acc)[::5]:
        tight = dse.reach(acc, scn, 0.25).set_index(["W", "g_s"]).nit
        loose = dse.reach(acc, scn, 1.0).set_index(["W", "g_s"]).nit
        assert set(tight.index) <= set(loose.index)
        assert (loose[tight.index] <= tight).all()


# --- the frontier ----------------------------------------------------------------------------


@pytest.mark.parametrize("seed", range(6))
def test_pareto_is_the_definition(seed):
    rng = np.random.default_rng(seed)
    # few distinct values per column, so ties and exact duplicates are common
    pts = rng.integers(0, 4 + seed, size=(120, 2 + seed % 4)).astype(float)
    assert (dse.pareto(pts) == _reference_pareto(pts)).all()


def test_pareto_keeps_one_of_equal_points():
    pts = np.array([[1.0, 2.0], [1.0, 2.0], [2.0, 1.0], [2.0, 2.0], [1.0, 2.0]])
    assert dse.pareto(pts).tolist() == [True, False, True, False, False]
    assert dse.pareto(np.empty((0, 3))).tolist() == []


def test_frontier_is_the_reference_on_a_sub_space(hw, acc):
    """Every candidate of one scenario with one format and one memory width: a few hundred
    designs, small enough for the pairwise reference."""
    cands = dse.candidates(hw, acc, ("qpsk", 64, 8), 0.5)
    cands = cands[(cands.W == cands.W.min()) & (cands.mem_dw == 64)]
    cands = cands[cands.g_s == cands.g_s.min()]
    assert 200 < len(cands) < 2000
    front = dse.frontier(cands)
    pool = cands[~cands.guarded & (cands.cmd_depth == dse.FRONTIER_CMD_DEPTH)]
    ref = _reference_pareto(pool[list(dse.OBJECTIVES)].to_numpy(float))
    assert list(front.order) == list(pool[ref].order)
    assert 1 < len(front) < len(pool)


def test_frontier_leaves_out_guarded_designs_and_other_queue_depths(hw, acc):
    cands = dse.candidates(hw, acc, ("qpsk", 128, 16), 1.0)
    assert (
        cands.guarded.any()
    )  # short jobs on fast designs: the model makes no claim there
    front = dse.frontier(cands)
    assert not front.guarded.any()
    assert set(front.cmd_depth) == {dse.FRONTIER_CMD_DEPTH}
    assert front.mem_ratio.min() >= dse.GUARD


# --- decisions -------------------------------------------------------------------------------


def test_pick_is_the_cheapest_design_that_meets_the_budget():
    cost = np.array([5.0, 3.0, 3.0, 1.0, 3.0])
    job = np.array([10.0, 30.0, 20.0, 90.0, 20.0])
    assert dse.pick(cost, job, np.inf) == 3
    assert dse.pick(cost, job, 50) == 2  # cost ties go to the faster, then to the first
    assert dse.pick(cost, job, 15) == 0
    assert dse.pick(cost, job, 5) is None
    assert dse.pick(cost, job, 50, eligible=[True, True, False, True, False]) == 1
    assert dse.pick(cost, job, 50, eligible=[False] * 5) is None
    assert dse.pick(cost, job, 20) == 2  # a design exactly on the budget meets it


# --- the committed tables --------------------------------------------------------------------


def test_committed_tables_are_what_the_code_regenerates(tmp_path):
    out = dse.write(tmp_path)
    for name in ("dse_frontier", "dse_scenarios"):
        assert out[name].read_bytes() == (dse.PAPER_DATA / f"{name}.csv").read_bytes()


def test_committed_frontier_is_consistent():
    front = pd.DataFrame(read_table(dse.PAPER_DATA / "dse_frontier.csv"))
    summary = read_table(dse.PAPER_DATA / "dse_scenarios.csv")
    assert len(summary) == 27 * len(dse.LOSS_BUDGETS_DB)
    sizes = front.groupby(["modulation", "M", "K", "budget_db"]).size()
    for r in summary:
        assert sizes[(r["modulation"], r["M"], r["K"], r["budget_db"])] == int(
            r["frontier"]
        )
        assert (
            int(r["joint_designs"])
            == {4: 110_160, 8: 220_320, 16: 345_600}[int(r["K"])]
        )
        assert int(r["candidates"]) > int(r["frontier"]) > 0
    assert sum(int(r["joint_designs"]) for r in summary) == 3 * dse.JOINT_DESIGNS
    assert (front.loss_db.astype(float) <= front.budget_db.astype(float)).all()
    assert set(front.cmd_depth) == {str(dse.FRONTIER_CMD_DEPTH)}
    assert (front.mem_ratio.astype(float) >= dse.GUARD).all()
    assert front.W.astype(int).max() <= 16
    # the two-objective fronts are parts of the five-objective one
    assert (
        front.front_dsp_job.astype(int)
        .groupby([front.modulation, front.M, front.K, front.budget_db])
        .sum()
        > 0
    ).all()
