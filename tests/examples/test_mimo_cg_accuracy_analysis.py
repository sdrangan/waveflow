"""Step 3.4 / AC3.2 of plans/mimo_cg/mimo_cg_paper_sims.md: SNR loss, the frontier, figures."""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from examples.mimo_cg import mimo_cg_accuracy_sweep as sweep
from examples.mimo_cg.mimo_cg import K_VALUES, M_VALUES, read_table
from examples.mimo_cg.mimo_cg_accuracy_analysis import (
    FLOOR,
    HERE,
    LOSS_BUDGET_DB,
    crossing_db,
    crossing_sigma_db,
    frontier_rows,
    loss_rows,
    rises_again,
)
from examples.mimo_cg.mimo_link import MODULATIONS

PAPER = HERE / "paper_data"
CASE = {"modulation": "qpsk", "M": "32", "K": "4", "residual": "recurrence"}


def _curve(det: str, bers, W="", g="", nit="", case=CASE) -> list[dict]:
    return [
        {
            **case,
            "rho_db": f"{float(s):.6e}",
            "detector": det,
            "W": str(W),
            "g_s": str(g),
            "nit": str(nit),
            "ber": f"{b:.6e}",
            "bit_errors": str(round(b * 1e6)),  # as if 1e6 bits per point
        }
        for s, b in enumerate(bers)
    ]


def test_crossing_interpolates_in_log_ber():
    assert crossing_db([(0.0, 1e-2), (1.0, 1e-4)]) == pytest.approx(0.5)
    assert crossing_db([(0.0, 1e-2), (1.0, 2e-3), (2.0, 5e-4)]) == pytest.approx(
        1 + math.log10(2) / math.log10(4)
    )
    assert crossing_db([(0.0, 1e-1), (1.0, 2e-3), (2.0, 1.5e-3)]) is None  # a floor


def test_curve_starting_below_target_is_an_error_not_a_floor():
    with pytest.raises(ValueError, match="window starts too high"):
        crossing_db([(0.0, 5e-4), (1.0, 1e-5)])


def test_losses_against_mmse_and_float_cg_keep_floors():
    rows = (
        _curve("mmse", [1e-2, 1e-4, 1e-6])  # crosses at 0.5 dB
        + _curve("cg2", [1e-2, 1e-3, 1e-5], nit=2)  # 1.0 dB
        + _curve("fx:W8g0:cg2", [1e-1, 1e-2, 2e-3], W=8, g=0, nit=2)  # a floor
        + _curve("fx:W12g0:cg2", [1e-2, 1e-2, 1e-4], W=12, g=0, nit=2)  # 1.5 dB
    )
    out = {r["detector"]: r for r in loss_rows(rows)}
    assert list(out) == ["mmse", "cg2", "fx:W8g0:cg2", "fx:W12g0:cg2"]
    assert out["mmse"]["loss_mmse_db"] == pytest.approx(0.0)
    assert out["cg2"]["loss_mmse_db"] == pytest.approx(0.5)
    assert out["cg2"]["loss_cg_db"] == ""  # float CG has no quantization loss
    floor = out["fx:W8g0:cg2"]
    assert floor["status"] == FLOOR and floor["snr_db"] == floor["loss_mmse_db"] == ""
    fx = out["fx:W12g0:cg2"]
    assert fx["status"] == "ok"
    assert fx["loss_mmse_db"] == pytest.approx(1.0)
    assert fx["loss_cg_db"] == pytest.approx(0.5)
    assert out["mmse"]["loss_sigma_db"] == 0.0 and floor["loss_sigma_db"] == ""
    assert fx["loss_sigma_db"] == pytest.approx(
        math.hypot(fx["snr_sigma_db"], out["mmse"]["snr_sigma_db"])
    )
    assert all(r["non_monotone"] == 0 for r in out.values())


def test_crossing_sigma_matches_a_poisson_monte_carlo():
    import numpy as np

    rng = np.random.default_rng(1)
    bits, bers = (1e5, 1e6), (1e-2, 1e-4)  # 1000 and 100 expected errors
    counts = rng.poisson([b * n for b, n in zip(bers, bits)], size=(20000, 2))
    xs = [crossing_db([(0.0, c0 / bits[0]), (1.0, c1 / bits[1])]) for c0, c1 in counts]
    expected = crossing_sigma_db([(0.0, 1e-2), (1.0, 1e-4)], [1000, 100])
    assert np.std(xs) == pytest.approx(expected, rel=0.05)
    assert crossing_sigma_db([(0.0, 1e-1), (1.0, 2e-3)], [10, 10]) is None  # a floor


def test_rises_again_flags_a_curve_that_climbs_back_above_target():
    assert rises_again([(0.0, 1e-2), (1.0, 5e-4), (2.0, 2e-3)])
    assert not rises_again([(0.0, 1e-2), (1.0, 5e-4), (2.0, 4e-4)])
    assert not rises_again([(0.0, 1e-1), (1.0, 2e-3)])  # never crosses


def _loss(W, g, nit, loss, case=CASE, sigma=0.01) -> dict:
    return {
        **case,
        "detector": f"fx:W{W}g{g}:cg{nit}",
        "W": W,
        "g_s": g,
        "nit": nit,
        "status": "ok",
        "snr_db": loss,
        "loss_mmse_db": loss,
        "loss_sigma_db": sigma,
        "loss_cg_db": loss,
    }


def test_frontier_is_the_non_dominated_set_with_a_lexicographic_headline():
    losses = [
        _loss(10, 4, 3, 0.4),
        _loss(10, 8, 2, 0.3),  # g_s larger but nit smaller: non-dominated
        _loss(12, 0, 4, 0.2),  # W larger but g_s smaller: non-dominated
        _loss(12, 4, 4, 0.1),  # dominated by (10, 4, 3)
        _loss(8, 0, 2, 0.6),  # outside the budget
        {**_loss(8, 4, 2, ""), "status": FLOOR},  # a floor never qualifies
    ]
    out = frontier_rows(losses)
    assert [(r["W"], r["g_s"], r["nit"], r["role"]) for r in out] == [
        (10, 4, 3, "headline"),
        (10, 8, 2, "frontier"),
        (12, 0, 4, "frontier"),
    ]


def test_fragile_designs_and_contenders_within_two_sigma_of_the_budget():
    losses = [
        _loss(10, 4, 3, 0.45, sigma=0.05),  # headline, within 2 sigma: fragile
        _loss(12, 0, 4, 0.20, sigma=0.05),  # frontier, clear of the budget
        _loss(
            8, 8, 3, 0.55, sigma=0.04
        ),  # outside but within 2 sigma, would be headline
        _loss(8, 4, 4, 0.70, sigma=0.05),  # outside by 4 sigma: not a contender
        _loss(12, 8, 2, 0.52, sigma=0.05),  # within 2 sigma, but would not be headline
    ]
    out = frontier_rows(losses)
    assert [(r["W"], r["g_s"], r["nit"], r["role"], r["fragile"]) for r in out] == [
        (10, 4, 3, "headline", 1),
        (12, 0, 4, "frontier", 0),
        (8, 8, 3, "contender", 1),
    ]


def test_a_case_with_no_design_in_budget_gets_a_none_row():
    other = {**CASE, "M": "128"}
    out = frontier_rows([_loss(10, 4, 3, 0.4), _loss(20, 8, 4, 0.7, case=other)])
    assert [r["role"] for r in out] == ["headline", "none"]
    assert out[1]["M"] == "128" and out[1]["W"] == ""


# --- the committed tables (step 3.4) ------------------------------------------------------

committed = pytest.mark.skipif(
    not (PAPER / "accuracy_frontier.csv").exists(),
    reason="run python -m examples.mimo_cg.mimo_cg_accuracy_analysis first",
)


@committed
def test_committed_losses_cover_every_detector_of_every_case():
    losses = read_table(PAPER / "accuracy_losses.csv")
    assert len(losses) == sum(sweep.expected_rows(c) for c in sweep.CONFIGS)
    for r in losses:
        assert (r["status"] == FLOOR) == (r["snr_db"] == "")
        if r["detector"] == "mmse":
            assert float(r["loss_mmse_db"]) == 0.0


@committed
def test_committed_frontier_matches_ac32():
    losses = read_table(PAPER / "accuracy_losses.csv")
    frontier = read_table(PAPER / "accuracy_frontier.csv")
    cases = {
        (r["modulation"], int(r["M"]), int(r["K"]), r["residual"]) for r in frontier
    }
    expected = {
        (mod, M, K, "recurrence")
        for mod in MODULATIONS
        for M in M_VALUES
        for K in K_VALUES
    }
    assert cases == expected | {
        (*sweep.SPOT_CHECK[2:], *sweep.SPOT_CHECK[:2], "explicit")
    }
    for case in cases:
        rows = [
            r
            for r in frontier
            if (r["modulation"], int(r["M"]), int(r["K"]), r["residual"]) == case
        ]
        roles = [r["role"] for r in rows]
        members = [r for r in rows if r["role"] in ("headline", "frontier")]
        assert roles[0] in ("headline", "none")
        assert roles == sorted(
            roles, key=["headline", "none", "frontier", "contender"].index
        )
        if roles[0] == "none":
            continue
        designs = [(int(r["W"]), int(r["g_s"]), int(r["nit"])) for r in members]
        assert all(float(r["loss_mmse_db"]) <= LOSS_BUDGET_DB for r in members)
        for r in rows:
            sigma = float(r["loss_sigma_db"])
            near = abs(float(r["loss_mmse_db"]) - LOSS_BUDGET_DB) <= 2 * sigma
            assert int(r["fragile"]) == int(near), r
            if r["role"] == "contender":
                assert near and float(r["loss_mmse_db"]) > LOSS_BUDGET_DB
                assert (int(r["W"]), int(r["g_s"]), int(r["nit"])) < designs[0]
        for a in designs:
            assert not any(
                b != a and all(x <= y for x, y in zip(b, a)) for b in designs
            )
        feasible = [
            (int(r["W"]), int(r["g_s"]), int(r["nit"]))
            for r in losses
            if (r["modulation"], int(r["M"]), int(r["K"]), r["residual"]) == case
            and r["detector"].startswith("fx:")
            and r["status"] == "ok"
            and float(r["loss_mmse_db"]) <= LOSS_BUDGET_DB
        ]
        assert designs[0] == min(feasible)


@committed
def test_figures_render_byte_identically(tmp_path: Path):
    from examples.mimo_cg.mimo_cg_accuracy_figures import write_accuracy_figures

    grid, losses = PAPER / "accuracy_grid.csv", PAPER / "accuracy_losses.csv"
    a = write_accuracy_figures(grid, losses, tmp_path / "a")
    b = write_accuracy_figures(grid, losses, tmp_path / "b")
    assert [p.name for p in a] == [p.name for p in b] and len(a) == 5
    for pa, pb in zip(a, b):
        assert pa.read_bytes() == pb.read_bytes(), pa.name
