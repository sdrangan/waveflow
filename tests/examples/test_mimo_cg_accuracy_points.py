"""The 30 accuracy points the CG model's move must leave byte-identical (gate 8.0).

The full check regenerates all 30 (``python -m examples.mimo_cg.tools.accuracy_points --check``,
about 8 minutes); here, the list is what gate 8.0 fixed, every point has its committed rows, and
the cheapest grid point and stress point regenerate byte-identically on every run.
"""

from __future__ import annotations

from examples.mimo_cg import mimo_cg_accuracy_sweep as S
from examples.mimo_cg.tools import accuracy_points as AP


def test_the_thirty_points():
    assert len(AP.GRID_POINTS) == 26 and len(set(AP.GRID_POINTS)) == 26
    assert len(AP.STRESS_POINTS) == 4 and len(set(AP.STRESS_POINTS)) == 4
    cases = {c for c, _ in AP.GRID_POINTS}
    assert {(S.CONFIGS[c].M, S.CONFIGS[c].K) for c in cases} == {
        (M, K) for M in (32, 64, 128) for K in (4, 8, 16)
    }
    assert any(S.CONFIGS[c].explicit for c in cases)
    refined = [p for p in AP.GRID_POINTS if AP.grid_budget(*p) == S.REFINE_MIN_ERRORS]
    assert 0 < len(refined) < len(AP.GRID_POINTS)
    assert {e for *_, e in AP.STRESS_POINTS} == {False, True}


def test_every_point_has_its_committed_rows():
    for case, offset in AP.GRID_POINTS:
        assert len(AP.committed_grid_lines(case, offset)) == S.expected_rows(case)
    for case, offset, explicit in AP.STRESS_POINTS:
        nits = S.CONFIGS[case].config.nits
        assert len(AP.committed_stress_lines(case, offset, explicit)) == 2 * len(nits)


def test_the_cheapest_points_regenerate_byte_identically():
    case, offset = "16qam_64x8", -6
    assert AP.grid_lines(case, offset, workers=1) == AP.committed_grid_lines(
        case, offset
    )
    point = ("16qam_64x8", 0, True)
    got = AP._lines(AP.stress_rows(*point))
    assert got == AP.committed_stress_lines(*point)
