"""Steps 6.3 and 6.5 of plans/mimo_cg/mimo_cg_paper_sims.md: decision fidelity.

Step 6.3 fixes the decision set before any brute-force build runs: the sub-grid, the job-time
budgets and the model's pick for every decision.  These tests pin that set to the committed file
and check that a pick is what the definition says: the cheapest candidate, by predicted numbers,
that the model may take within the budget.
"""

from __future__ import annotations

from collections import Counter

import numpy as np
import pytest

from examples.mimo_cg.hw import dse, space
from examples.mimo_cg.hw import fidelity as F
from examples.mimo_cg.mimo_cg import read_table


@pytest.fixture(scope="module")
def hw():
    return F.subgrid_table()


@pytest.fixture(scope="module")
def acc():
    return dse.accuracy()


@pytest.fixture(scope="module")
def committed():
    return read_table(F.DECISIONS)


# --- the decision set (step 6.3) --------------------------------------------------------------


def test_subgrid_table_is_the_grid_with_predictions(hw):
    grid = space.bruteforce_grid()
    assert len(hw) == 1440 and list(hw.order) == list(range(1440))
    assert list(hw.build) == [space.bf_label(c) for c in grid]
    assert [tuple(int(v) for v in r) for r in hw[list(dse.KNOBS)].to_numpy()] == [
        tuple(getattr(c, k) for k in dse.KNOBS) for c in grid
    ]
    whole = dse.hw_table()
    merged = hw.merge(whole, on=list(dse.KNOBS), suffixes=("", "_all"))
    assert len(merged) == 1440  # every sub-grid design is a design of the space
    for col in (*dse.RESOURCES, "t0", "t_iter"):
        assert (merged[col] == merged[f"{col}_all"]).all()


def test_committed_decisions_are_what_the_code_regenerates(tmp_path):
    again = F.write_decisions(tmp_path / "bruteforce_decisions.csv")
    assert again.read_bytes() == F.DECISIONS.read_bytes()


def test_the_decision_set(committed):
    """27 scenarios × 3 loss budgets × 8 job-time budgets × 4 resources."""
    assert len(committed) == 27 * 3 * F.N_JOB_BUDGETS * 4 == 2592
    assert Counter(r["resource"] for r in committed) == dict.fromkeys(
        dse.RESOURCES, 648
    )
    assert {float(r["budget_db"]) for r in committed} == set(dse.LOSS_BUDGETS_DB)
    head = F.DECISIONS.read_text(encoding="utf-8").splitlines()[0]
    assert "model_sha256=d95510d337e992d3" in head and "guard=2.0" in head
    # scenarios that ask the same thing of the hardware share a problem number
    problems = {}
    for r in committed:
        key = (r["K"], r["job_budget"], r["resource"], r["pick"], r["pick_nit"])
        assert problems.setdefault(r["problem"], key) == key
    assert len(problems) == 1728
    grid = {space.bf_label(c) for c in space.bruteforce_grid()}
    picks = {r["pick"] for r in committed}
    assert picks <= grid and len(picks) == 333


def test_job_budgets_span_the_candidates(hw, acc, committed):
    for scn in dse.scenarios(acc)[::4]:
        for budget in dse.LOSS_BUDGETS_DB:
            cands = dse.candidates(hw, acc, scn, budget)
            taus = F.job_budgets(cands)
            assert len(taus) == F.N_JOB_BUDGETS and taus == sorted(set(taus))
            job = cands.job[~cands.guarded]
            assert taus[0] == pytest.approx(F.BUDGET_MARGIN * job.min(), abs=1)
            assert taus[-1] == pytest.approx(F.BUDGET_MARGIN * job.max(), abs=1)
            ratios = np.diff(np.log(taus))
            assert ratios.max() - ratios.min() < 0.01  # geometric
            mine = [
                r
                for r in committed
                if (r["modulation"], int(r["M"]), int(r["K"])) == scn
                and float(r["budget_db"]) == budget
            ]
            assert sorted({int(r["job_budget"]) for r in mine}) == taus


def test_a_pick_is_the_cheapest_eligible_candidate(hw, acc, committed):
    """Checked against the definition, design by design, for a spread of decisions."""
    seen = 0
    for r in committed[::37]:
        scn = (r["modulation"], int(r["M"]), int(r["K"]))
        cands = dse.candidates(hw, acc, scn, float(r["budget_db"]))
        tau, res = int(r["job_budget"]), r["resource"]
        ok = cands[~cands.guarded & (cands.job <= tau)]
        assert len(ok) == int(r["eligible"]) >= 1 and len(cands) == int(r["candidates"])
        best = ok[ok[res] == ok[res].min()]
        best = best[best.job == best.job.min()]
        mine = cands[cands.build == r["pick"]].iloc[0]
        assert best.iloc[0]["build"] == r["pick"]
        assert int(mine[res]) == int(r["pick_cost"]) == int(ok[res].min())
        assert int(mine["nit"]) == int(r["pick_nit"])
        assert not mine["guarded"] and mine["job"] <= tau
        seen += 1
    assert seen > 60


def test_the_model_never_picks_a_guarded_design(hw, acc, committed):
    guarded = set()
    for scn in dse.scenarios(acc):
        cands = dse.candidates(hw, acc, scn, 1.0)
        guarded |= {
            (b, n)
            for b, n in zip(
                cands.build[cands.guarded], cands.nit[cands.guarded], strict=True
            )
        }
    assert guarded  # at 1 dB some fast designs run so few iterations that memory binds
    assert not {(r["pick"], int(r["pick_nit"])) for r in committed} & guarded


# --- scoring (step 6.5), on made-up measurements ----------------------------------------------


def _cands(rows: list[tuple]) -> object:
    """Candidates from ``(build, predicted lut, predicted job, guarded, measured lut, measured
    job)``; the other resources copy the LUT column."""
    import pandas as pd

    df = pd.DataFrame(
        rows, columns=["build", "lut", "job", "guarded", "m_lut", "m_job"]
    )
    for res in ("dsp", "ff", "bram"):
        df[res], df[f"m_{res}"] = df.lut, df.m_lut
    return df


def test_judge_right_when_the_pick_is_the_brute_force_pick():
    cands = _cands(
        [
            ("a", 100, 50, False, 104, 51),
            ("b", 80, 90, False, 82, 91),
            ("c", 60, 200, False, 61, 199),
        ]
    )
    got = F.judge(cands, "lut", 100)
    assert (got["pick"], got["bf_pick"]) == ("b", "b")
    assert got["right"] == got["same_build"] == got["same_cost"] == got["met"] == 1
    assert got["regret_pct"] == 0 and (got["m_cost"], got["m_job"]) == (82, 91)


def test_judge_cost_tolerance_is_ten_percent_of_the_brute_force_cost():
    # the model prefers b (predicted 80 < 81), which really costs more than a
    near = _cands([("a", 81, 50, False, 100, 50), ("b", 80, 60, False, 110, 60)])
    got = F.judge(near, "lut", 100)
    assert (got["pick"], got["bf_pick"], got["regret_pct"]) == ("b", "a", 10.0)
    assert got["right"] == 1 and got["same_build"] == 0 and got["same_cost"] == 0
    far = _cands([("a", 81, 50, False, 100, 50), ("b", 80, 60, False, 111, 60)])
    got = F.judge(far, "lut", 100)
    assert got["regret_pct"] == 11.0 and got["right"] == 0 and got["met"] == 1
    # an equally cheap design is the same decision, though not the same build
    tie = _cands([("a", 81, 50, False, 100, 50), ("b", 80, 60, False, 100, 60)])
    got = F.judge(tie, "lut", 100)
    assert got["right"] == 1 and got["same_build"] == 0 and got["same_cost"] == 1


def test_judge_job_tolerance_is_two_percent_of_the_budget():
    # predicted within the budget, measured 2% over it: still met; 3% over: missed
    ok = _cands([("a", 100, 50, False, 100, 50), ("b", 80, 99, False, 80, 102)])
    got = F.judge(ok, "lut", 100)
    assert (got["pick"], got["bf_pick"], got["met"]) == ("b", "a", 1)
    assert (
        got["right"] == 1 and got["regret_pct"] == -20.0
    )  # cheaper than any design in budget
    late = _cands([("a", 100, 50, False, 100, 50), ("b", 80, 99, False, 80, 103)])
    got = F.judge(late, "lut", 100)
    assert got["met"] == 0 and got["right"] == 0 and got["same_cost"] == 0


def test_judge_the_guard_binds_the_model_only():
    """The model may not take a guarded design; the brute force may, and if that design is the
    best one the model's pick is judged against it."""
    cands = _cands([("fast", 50, 10, True, 50, 12), ("slow", 100, 40, False, 100, 40)])
    got = F.judge(cands, "lut", 60)
    assert (got["pick"], got["bf_pick"], got["regret_pct"]) == ("slow", "fast", 100.0)
    assert got["right"] == 0 and got["met"] == 1


def test_judge_without_a_measurement():
    nan = float("nan")
    # the model's pick did not build: not right, and counted as unmeasured
    lost = _cands([("a", 100, 50, False, 100, 50), ("b", 80, 60, False, nan, nan)])
    got = F.judge(lost, "lut", 100)
    assert (got["pick"], got["bf_pick"], got["measured"], got["right"]) == (
        "b",
        "a",
        0,
        0,
    )
    # no design is measured within the budget: the pick stands on the job tolerance alone
    none = _cands([("a", 100, 99, False, 100, 101), ("b", 80, 300, False, 80, 300)])
    got = F.judge(none, "lut", 100)
    assert (got["pick"], got["bf_pick"], got["met"], got["right"]) == ("a", "", 1, 1)
    assert got["regret_pct"] == ""
    # nothing the model may take: no pick, not right
    got = F.judge(_cands([("a", 100, 500, False, 100, 500)]), "lut", 100)
    assert got["pick"] == "" and got["right"] == 0


def test_score_on_a_perfect_brute_force_is_all_right(hw, acc, committed):
    """If the measurements equalled the predictions, every decision would be right — except
    where the best design is one the guard keeps from the model."""
    import pandas as pd

    meas = pd.DataFrame({"build": hw.build} | {k: hw[k] for k in dse.RESOURCES})
    meas = meas.set_index("build")
    for nits in space.NITS.values():
        for nit in nits:
            meas[f"job{nit}"] = (hw.t0 + nit * hw.t_iter).to_numpy()
    rows = F.score(hw, acc, meas, committed)
    assert len(rows) == 2592 and all(r["measured"] and r["met"] for r in rows)
    wrong = [r for r in rows if not r["right"]]
    assert all(r["pick"] != r["bf_pick"] for r in wrong)
    assert all(
        float(r["budget_db"]) == 1.0 for r in wrong
    )  # the guard only binds at 1 dB
    table = {(m["set"], m["resource"]): m for m in F.metrics(rows)}
    assert set(table) == {("all", r) for r in (*dse.RESOURCES, "any")}
    assert table[("all", "any")]["decisions"] == 2592
    assert (
        table[("all", "dsp")]["threshold"] == ">= 90"
        and table[("all", "any")]["threshold"] == ""
    )
    assert all(
        m["missed_budget_pct"] == 0 and m["unmeasured"] == 0 for m in table.values()
    )
    # leaving builds out re-asks every decision of what remains, under the same budgets
    some = frozenset(hw.build[::7])
    again = F.score(hw, acc, meas, committed, exclude=some)
    assert len(again) == 2592 and not {r["pick"] for r in again} & some


# --- the learning curve (step 6.6) -------------------------------------------------------------


def test_curve_subsets_are_seeded_stratified_and_keep_the_core():
    pool = F.calibration_builds()
    assert {top: len(v) for top, v in pool.items()} == {"vec": 25, "mm": 45, "det": 16}
    assert set(F.CURVE_CORE) <= set(pool["det"])
    want = {10: (3, 5, 2), 20: (6, 10, 4), 30: (9, 16, 5), 45: (13, 24, 8)}
    for n in F.CURVE_SIZES:
        seen = set()
        for draw in range(F.CURVE_DRAWS):
            sub = F.curve_subset(n, draw, pool)
            assert sub == F.curve_subset(n, draw, pool) and len(sub) == n
            assert set(F.CURVE_CORE) <= sub
            got = tuple(
                sum(b in sub for b in pool[top]) for top in ("vec", "mm", "det")
            )
            assert got == want[n]
            seen.add(sub)
        assert len(seen) == F.CURVE_DRAWS  # twenty different subsets


def test_a_refit_on_a_subset_reads_only_that_subset():
    from examples.mimo_cg.hw import models as MD

    pool = F.calibration_builds()
    sub = F.curve_subset(30, 3, pool)
    m = MD.fit(builds=sub, loo=False)
    assert m.meta["fit_builds"] == 30
    assert m.report["CgVec.lut"]["n"] == 9 and m.report["CgMm.lut"]["n"] == 16
    assert "loo_mape_pct" not in m.report["CgVec.lut"]
    # the two memory word widths can be priced, which is what the core detectors are for
    assert {"MemRStream|32", "MemRStream|64", "adapters|32", "adapters|64"} <= set(
        m.table
    )
    hw = F.subgrid_table(m)
    assert len(hw) == 1440 and np.isfinite(hw[list(dse.RESOURCES)].to_numpy()).all()
    # every build of the pool is the committed fit, whatever the leave-one-out switch
    full = MD.fit(builds=frozenset(b for v in pool.values() for b in v), loo=False)
    committed = MD.Models.load()
    assert full.table == committed.table
    for name, co in committed.coef.items():
        assert full.coef[name] == pytest.approx(co, rel=1e-9, abs=1e-9)


# --- beside the decisions: frontiers and errors, on made-up measurements -----------------------


def _perfect(hw):
    """A measured table that equals the predictions."""
    import pandas as pd

    meas = pd.DataFrame({"build": hw.build} | {k: hw[k] for k in dse.RESOURCES})
    meas = meas.set_index("build")
    for nits in space.NITS.values():
        for nit in nits:
            meas[f"job{nit}"] = (hw.t0 + nit * hw.t_iter).to_numpy()
    return meas


def test_frontier_overlap_is_total_when_measurement_equals_prediction(hw, acc):
    rows = F.frontier_overlap(hw, acc, _perfect(hw))
    assert len(rows) == 27 * 3 * 4
    tight = [r for r in rows if r["budget_db"] < 1.0]  # no guarded candidate below 1 dB
    assert all(
        r["precision_pct"] == r["recall_pct"] == r["covered_pct"] == 100.0
        for r in tight
    )
    assert all(r["predicted"] == r["measured"] == r["both"] for r in tight)
    # at 1 dB the guard keeps a few fast designs off the predicted frontier: recall can drop,
    # precision cannot rise above what is there, and nothing is predicted that is not real
    loose = [r for r in rows if r["budget_db"] == 1.0]
    assert all(r["both"] <= r["predicted"] <= r["candidates"] for r in loose)
    assert min(r["recall_pct"] for r in loose) < 100.0


def test_model_errors_and_their_summary(hw):
    meas = _perfect(hw)
    meas["lut"] = (
        meas["lut"] * 1.10
    )  # every LUT count measured 10% above the prediction
    meas.iloc[0, meas.columns.get_loc("dsp")] += 1  # and one DSP count off by one
    errors = F.model_errors(
        hw, meas.drop(index=meas.index[5])
    )  # one build not measured
    builds = {e["build"] for e in errors}
    assert len(builds) == 1439 and hw.build[5] not in builds
    per_build = {K: 4 + len(n) for K, n in space.NITS.items()}
    assert len(errors) == sum(per_build[K] for K in hw.K.drop(index=5))
    table = {m["metric"]: m for m in F.error_metrics(errors)}
    assert table["LUT MAPE (%)"]["value"] == pytest.approx(
        100 * (1 / 1.1 - 1) * -1, abs=0.01
    )
    assert (
        table["FF MAPE (%)"]["value"] == 0 and table["BRAM exact (%)"]["value"] == 100.0
    )
    assert table["DSP exact (%)"]["value"] == pytest.approx(
        100 * 1438 / 1439, abs=0.001
    )
    assert table["job time MAPE (%), loop-dominated"]["value"] == pytest.approx(
        0, abs=1e-3
    )
    # the guard's verdict is recorded per job, and the two kinds are summarised apart
    guarded = [e for e in errors if e["quantity"] == "job" and e["guarded"]]
    # 193 of the sub-grid's 9,000 jobs: 16-lane designs running at most three iterations
    assert len(guarded) == 193 - sum(
        e["guarded"]
        for e in F.model_errors(hw[hw.build == hw.build[5]], _perfect(hw))
        if e["quantity"] == "job"
    )
    assert all(e["L"] == 16 and e["nit"] <= 3 for e in guarded)
    assert table["job time MAPE (%), under the guard"]["n"] == len(guarded)
    assert {m for m in table if "lanes" in m} == {
        f"LUT MAPE (%), {lanes} lanes" for lanes in (1, 4, 16)
    }


def test_figures_are_deterministic(tmp_path, hw, acc, committed):
    from examples.mimo_cg.hw import fidelity_figure as FF

    rows = F.score(hw, acc, _perfect(hw), committed)
    table = F.metrics(rows)
    a = FF.render_decisions(rows, table, tmp_path / "a.svg").read_bytes()
    b = FF.render_decisions(rows, table, tmp_path / "b.svg").read_bytes()
    assert a == b and a.startswith(b"<?xml")
    curve = [
        {"builds": n, "draw": d, "resource": res, "right_pct": 50 + n / 2 + d}
        for n in (10, 20, 86)
        for d in range(1 if n == 86 else 3)
        for res in (*dse.RESOURCES, "any")
    ]
    a = FF.render_curve(curve, tmp_path / "c.svg").read_bytes()
    assert a == FF.render_curve(curve, tmp_path / "d.svg").read_bytes()
