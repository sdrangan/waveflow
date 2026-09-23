"""Scoring gates independent of paid inference."""
from examples.dse_fir.benchmark import best, grid, score
from examples.dse_fir.contracts import identity


def row(label, quality, feasible=True):
    return {"candidate_id": label, "params": {}, "quality": {"stopband_rej_db": quality},
            "feasible": feasible}


def test_reference_grid_has_24_distinct_points():
    points = grid()
    assert len(points) == len({identity(p) for p in points}) == 24
    assert all(p["samp_i"] == 2 and p["mem_dwidth"] == 32 for p in points)


def test_score_uses_feasible_evidence_and_handles_no_solution():
    optimum = row("optimum", 40)
    tempting = row("not_measured", 50, None)
    assert best([tempting, optimum]) == optimum
    assert score([tempting], optimum)["regret_db"] is None
    assert score([row("worse", 35)], optimum)["regret_db"] == 5
    assert score([row("tie", 40)], optimum)["optimum_found"]
